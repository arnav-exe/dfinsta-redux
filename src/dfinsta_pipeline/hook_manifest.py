"""Version-independent hook intent, and the engine that resolves it onto a target.

The patch manifests this project shipped so far (`dfinsta_source_430`,
`dfinsta_source_439`) are *resolved* artifacts: they name concrete obfuscated
classes and concrete registers, so every one had to be rewritten by hand for a new
Instagram version. This module holds the layer above that — what a hook means,
expressed so it can be resolved against any version.

The design is empirical. Diffing the same seven hooks as resolved against
Instagram 430 and 439 showed exactly what varies:

    set_app_context     only the register moved (v0 -> v4)
    tigon_url_block     only the request parameter's type moved (LX/05ez -> LX/03AS)
    replace_reels_*     only the owning class moved (LX/05t2 -> LX/04tC)
    settings hooks      class, registers and anchor text all moved

So an anchor is written as a *pattern* with named captures for the parts that move,
and the payload is a template referring to those captures. Five of the seven hooks
then resolve mechanically, with no agent involved at all.

Capture syntax is ``<name:kind>``. Angle brackets are used rather than braces
because smali register lists are already written ``{v0, v1}`` and would collide.

    <app:reg>       a register            v0, p1
    <cls:type>      a type descriptor     LX/05ez;  Lcom/instagram/Foo;
    <fld:member>    a field or method     A08, onCreate
    <x:any>         one non-space token

``<init>`` and ``<clinit>`` are reserved and always literal, because smali writes
constructors that way and they would otherwise parse as captures.

A later reference to the same name, written ``<name>``, must match the value
captured earlier — so ``move-result-object <r>`` provably lands in the same
register the literal was loaded into.

What this module does NOT do: choose between candidate hosts, or judge whether a
literal is really the outgoing request path. Those need search and judgement, and
belong to the Resolve stage's agents. This module is the deterministic half — it
turns a fingerprint plus a decode into a concrete, checked operation, or fails.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# `init` and `clinit` are RESERVED: smali writes constructors as `-><init>(...)V`,
# which would otherwise parse as a capture. They are always literal text.
CAPTURE = re.compile(
    r"<(?!init[>:]|clinit[>:])(?P<name>[a-z_][a-z0-9_]*)(?::(?P<kind>reg|type|member|any))?>"
)
RESERVED_CAPTURE_NAMES = frozenset({"init", "clinit"})

KIND_PATTERNS = {
    "reg": r"[vp]\d+",
    "type": r"\[*(?:L[^;\s]+;|[ZBCSIJFD])",
    "member": r"[A-Za-z_$][A-Za-z0-9_$]*",
    "any": r"\S+",
}


PROBE_KINDS = frozenset({"logcat_delta", "ui_dialog", "startup_no_fatal"})


class ManifestError(ValueError):
    """Raised when a manifest is malformed or cannot resolve against a target."""


def compile_pattern(
    line: str, kinds: dict[str, str] | None = None
) -> tuple[re.Pattern[str], list[str]]:
    """Turn one anchor pattern line into a regex plus the capture names it binds.

    `kinds` carries capture kinds declared on EARLIER anchor lines. A repeat within
    the same line becomes a regex backreference; a repeat across lines cannot, since
    each line is matched separately, so it re-captures and `resolve_in_source`
    enforces that both occurrences bound the same value. Either way "the result is
    written back to the SAME register" stays checkable rather than assumed.
    """
    kinds = {} if kinds is None else kinds
    out: list[str] = ["^"]
    order: list[str] = []
    seen: dict[str, int] = {}
    position = 0
    for match in CAPTURE.finditer(line):
        out.append(re.escape(line[position : match.start()]))
        name = match.group("name")
        kind = match.group("kind")
        if kind and name in kinds and kinds[name] != kind:
            raise ManifestError(
                f"capture {name!r} is declared as {kinds[name]!r} and later as {kind!r}; "
                "one name must have one kind across the whole anchor"
            )
        if name in seen:
            if kind:
                raise ManifestError(
                    f"capture {name!r} is re-declared with a kind; later uses must be bare <{name}>"
                )
            out.append(f"(?P=g{seen[name]})")
        else:
            if not kind:
                kind = kinds.get(name)
                if not kind:
                    raise ManifestError(
                        f"first use of capture {name!r} must declare a kind"
                    )
            index = len(seen) + 1
            seen[name] = index
            order.append(name)
            kinds.setdefault(name, kind)
            out.append(f"(?P<g{index}>{KIND_PATTERNS[kind]})")
        position = match.end()
    out.append(re.escape(line[position:]))
    out.append("$")
    return re.compile("".join(out)), order


def compile_anchor(lines: Iterable[str]) -> list[tuple[re.Pattern[str], list[str]]]:
    """Compile every anchor line, sharing capture kinds across the whole anchor."""
    kinds: dict[str, str] = {}
    return [compile_pattern(line, kinds) for line in lines]


def render(line: str, bindings: dict[str, str]) -> str:
    """Substitute captured values back into a payload template line."""

    def swap(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in bindings:
            raise ManifestError(f"payload references unbound capture <{name}>")
        return bindings[name]

    return CAPTURE.sub(swap, line)


@dataclass(frozen=True)
class HostFingerprint:
    """How to find the class a hook attaches to, without naming it.

    ``co_literals`` are the *other* API-path literals the host must also
    contain. They exist because a single literal is not selective enough: each
    Reels endpoint string appears in 2-5 classes on both 430 and 439 — analytics
    maps and prefetch allowlists carry them too — and the hook's anchor matches
    cleanly inside three of them. Only the class that builds the outgoing
    request path carries all three endpoints, so co-location is what actually
    picks the host out. Measured, not assumed; and if a version splits them the
    intersection empties and the caller escalates rather than guessing.
    """

    kind: str  # "named" | "by_literal" | "by_agent"
    descriptor: str | None = None  # kind == "named"
    literal: str | None = None  # kind == "by_literal"
    co_literals: tuple[str, ...] = ()  # kind == "by_literal"
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"named", "by_literal", "by_agent"}:
            raise ManifestError(f"unknown host fingerprint kind {self.kind!r}")
        if self.kind == "named" and not self.descriptor:
            raise ManifestError("named fingerprint needs a descriptor")
        if self.kind == "by_literal" and not self.literal:
            raise ManifestError("by_literal fingerprint needs a literal")
        if self.co_literals and self.kind != "by_literal":
            raise ManifestError(
                f"co_literals only apply to a by_literal fingerprint, not {self.kind!r}"
            )
        if self.literal in self.co_literals:
            raise ManifestError(
                f"co_literal {self.literal!r} repeats the primary literal; "
                "co_literals are the OTHER literals the host must also contain"
            )
        if len(set(self.co_literals)) != len(self.co_literals):
            raise ManifestError("co_literals contains a duplicate")

    @property
    def required_literals(self) -> tuple[str, ...]:
        """Every literal the host must contain, primary first."""
        if self.kind != "by_literal":
            return ()
        assert self.literal is not None  # guaranteed by __post_init__
        return (self.literal, *self.co_literals)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostFingerprint:
        return cls(
            data["kind"],
            data.get("descriptor"),
            data.get("literal"),
            tuple(data.get("co_literals", ())),
            data.get("note", ""),
        )


@dataclass(frozen=True)
class Probe:
    """How to prove at runtime that this hook actually does something.

    ``requires_two_directional_delta`` is not decoration. A probe that shows no
    signal with the toggle on AND none with it off reads like a pass but actually
    means the probe cannot see this hook — which is exactly what block-counting
    does to Reels, because the endpoint is blanked upstream of the block.
    """

    kind: str
    signal: str
    surface: str
    requires_two_directional_delta: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PROBE_KINDS:
            raise ManifestError(
                f"unknown probe kind {self.kind!r}; an unrecognised probe reaching the "
                "Verify stage is exactly what this class exists to prevent"
            )
        if not self.signal.strip() or not self.surface.strip():
            raise ManifestError("probe needs a non-empty signal and surface")
        if not self.requires_two_directional_delta and not self.note.strip():
            raise ManifestError(
                "waiving requires_two_directional_delta needs a note saying why; "
                "silent waivers are how an inert hook passes verification"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Probe:
        return cls(
            data["kind"],
            data["signal"],
            data["surface"],
            bool(data.get("requires_two_directional_delta", True)),
            data.get("note", ""),
        )


@dataclass(frozen=True)
class Hook:
    hook_id: str
    intent: str
    tier: str  # robust | fragile | ui
    strategy: str
    semantic_deps: tuple[str, ...]
    hosts: tuple[HostFingerprint, ...]
    anchor: tuple[str, ...]
    payload: tuple[str, ...]
    marker: str
    expected_marker_count: int
    mode: str = "insert_after"
    # Fixed at 1. Retained because the emitted operation must carry it for the
    # applier; a hook that needs several sites gets one manifest entry per site.
    expected_anchor_count: int = 1
    # What last version's patch looked like: notes for the applier and for a
    # human reviewing a diff. NOT safe to show a proposer — they describe the
    # answer's shape, and the shape is exactly what changes between versions.
    constraints: tuple[str, ...] = ()
    # What the patch must ACHIEVE, in user-visible terms. Safe to show a
    # proposer, because it constrains the outcome without describing the code.
    # Kept separate from `constraints` because mixing them turns a search into a
    # reading-comprehension exercise and makes any measurement of the proposer
    # worthless.
    intent_constraints: tuple[str, ...] = ()
    probe: Probe | None = None
    status: str = "active"
    # When true the anchor and payload here are a SHAPE, not a complete patch,
    # and a validated proposal must supply the real ones. Set for the profile
    # settings hook, whose payload needs an own-profile guard on a register no
    # anchor can capture: rendering the template as written would attach the
    # long-press to every profile's Options and undo the device-verified
    # other-user exclusion.
    requires_proposal: bool = False

    def __post_init__(self) -> None:
        if self.tier not in {"robust", "fragile", "ui"}:
            raise ManifestError(f"{self.hook_id}: unknown tier {self.tier!r}")
        if self.mode not in {"insert_after", "replace"}:
            raise ManifestError(f"{self.hook_id}: unknown mode {self.mode!r}")
        if not self.hosts:
            raise ManifestError(f"{self.hook_id}: needs at least one host fingerprint")
        if not self.anchor:
            raise ManifestError(f"{self.hook_id}: needs an anchor")
        if not self.payload:
            raise ManifestError(f"{self.hook_id}: needs a payload")
        # An empty marker is silently fatal: str.count("") returns len+1, so the
        # already-applied guard would fire on every class and the hook could never
        # resolve. Reject it here rather than debug it later.
        if not self.marker.strip():
            raise ManifestError(f"{self.hook_id}: marker must be a non-empty string")
        if self.expected_marker_count < 1:
            raise ManifestError(f"{self.hook_id}: expected_marker_count must be >= 1")
        if self.expected_anchor_count != 1:
            # With N>1 only the first hit's bindings render the anchor, but the
            # emitted operation still declares N. The applier does a literal search,
            # so if the hits bound different registers it finds 1 of N and refuses.
            raise ManifestError(
                f"{self.hook_id}: expected_anchor_count must be 1; "
                "multi-site hooks need one entry per site"
            )
        in_payload = "\n".join(self.payload).count(self.marker)
        if in_payload != self.expected_marker_count:
            raise ManifestError(
                f"{self.hook_id}: marker {self.marker!r} appears {in_payload} time(s) in its "
                f"own payload but expected_marker_count is {self.expected_marker_count}; "
                "the applier would refuse this patch forever"
            )
        # fail early on an unusable pattern rather than at resolve time
        declared: set[str] = set()
        for _, names in compile_anchor(self.anchor):
            declared.update(names)
        for line in self.payload:
            for match in CAPTURE.finditer(line):
                if match.group("name") not in declared:
                    raise ManifestError(
                        f"{self.hook_id}: payload uses <{match.group('name')}> "
                        "which no anchor line captures"
                    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hook:
        return cls(
            hook_id=data["hook_id"],
            intent=data["intent"],
            tier=data["tier"],
            strategy=data["strategy"],
            semantic_deps=tuple(data.get("semantic_deps", ())),
            hosts=tuple(HostFingerprint.from_dict(h) for h in data["hosts"]),
            anchor=tuple(data["anchor"]),
            payload=tuple(data["payload"]),
            marker=data["marker"],
            expected_marker_count=int(data["expected_marker_count"]),
            mode=data.get("mode", "insert_after"),
            expected_anchor_count=int(data.get("expected_anchor_count", 1)),
            constraints=tuple(data.get("constraints", ())),
            intent_constraints=tuple(data.get("intent_constraints", ())),
            probe=Probe.from_dict(data["probe"]) if data.get("probe") else None,
            status=data.get("status", "active"),
            requires_proposal=bool(data.get("requires_proposal", False)),
        )


@dataclass
class Resolution:
    """One hook resolved against one target, or the reason it could not be."""

    hook_id: str
    resolved: bool
    already_applied: bool = False
    descriptor: str | None = None
    smali_path: str | None = None
    anchor: list[str] = field(default_factory=list)
    payload: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)
    occurrences: int = 0
    reason: str = ""

    def as_operation(self, hook: Hook) -> dict[str, Any]:
        """Emit the resolved form the existing applier already understands."""
        if not self.resolved:
            raise ManifestError(f"{self.hook_id} is unresolved: {self.reason}")
        return {
            "id": self.hook_id,
            "descriptor": self.descriptor,
            "mode": hook.mode,
            "anchor": list(self.anchor),
            "expected_anchor_count": hook.expected_anchor_count,
            "marker": hook.marker,
            "expected_marker_count": hook.expected_marker_count,
            "payload": list(self.payload),
        }


def significant(lines: Iterable[str]) -> list[tuple[int, str]]:
    """Non-blank, non-.line, non-comment lines, matching the applier's own view."""
    out = []
    for index, raw in enumerate(lines):
        text = raw.strip()
        if text and not text.startswith(".line") and not text.startswith("#"):
            out.append((index, text))
    return out


def resolve_in_source(hook: Hook, descriptor: str, smali: str) -> Resolution:
    """Match the hook's anchor pattern inside one class body."""
    # Marker first, matching the applier's own order. An exact count is the normal
    # already-applied state, NOT a failure; only a partial count is wrong. Checking
    # this after anchor matching would report "anchor did not match" for a genuinely
    # partial patch whose anchor got mangled.
    marker_count = smali.count(hook.marker)
    if marker_count == hook.expected_marker_count:
        return Resolution(
            hook.hook_id,
            False,
            already_applied=True,
            descriptor=descriptor,
            reason=f"already applied: marker {hook.marker!r} present {marker_count} time(s)",
        )
    if marker_count:
        return Resolution(
            hook.hook_id,
            False,
            descriptor=descriptor,
            reason=(
                f"marker {hook.marker!r} found {marker_count}/"
                f"{hook.expected_marker_count} times; partially applied"
            ),
        )

    body = significant(smali.splitlines())
    compiled = compile_anchor(hook.anchor)
    width = len(compiled)
    hits: list[tuple[int, dict[str, str]]] = []

    for start in range(len(body) - width + 1):
        bindings: dict[str, str] = {}
        ok = True
        for offset, (pattern, names) in enumerate(compiled):
            match = pattern.match(body[start + offset][1])
            if not match:
                ok = False
                break
            for position, name in enumerate(names, start=1):
                value = match.group(f"g{position}")
                if name in bindings and bindings[name] != value:
                    ok = False
                    break
                bindings[name] = value
            if not ok:
                break
        if ok:
            hits.append((start, bindings))

    if not hits:
        return Resolution(
            hook.hook_id, False, descriptor=descriptor, reason="anchor pattern did not match"
        )
    if len(hits) != hook.expected_anchor_count:
        return Resolution(
            hook.hook_id,
            False,
            descriptor=descriptor,
            occurrences=len(hits),
            reason=(
                f"anchor matched {len(hits)} times, expected "
                f"{hook.expected_anchor_count}; the pattern is ambiguous in this class"
            ),
        )

    _, bindings = hits[0]
    concrete_anchor = [render(line, bindings) for line in hook.anchor]
    concrete_payload = [render(line, bindings) for line in hook.payload]
    return Resolution(
        hook.hook_id,
        True,
        descriptor=descriptor,
        anchor=concrete_anchor,
        payload=concrete_payload,
        bindings=bindings,
        occurrences=len(hits),
    )


def assert_distinct(hooks: Iterable[Hook]) -> None:
    """Every hook must have its own id and its own idempotence marker.

    The marker rule is the load-bearing one. A marker identifies ONE hook's
    patch, so when two hooks share one, each reads the other's applied patch as
    its own and reports already-applied — and both silently drop out of the build
    while the run still reports complete. Two hooks in this repo's own manifest
    really did share `Lcom/dfinstagram/SettingsWrapper;`.

    Checked here rather than only in :func:`load_manifest`, because any caller
    can assemble a hook list without going near a file.
    """
    ids: set[str] = set()
    markers: dict[str, str] = {}
    for hook in hooks:
        if hook.hook_id in ids:
            raise ManifestError(f"duplicate hook_id {hook.hook_id!r}")
        ids.add(hook.hook_id)
        if hook.marker in markers:
            raise ManifestError(
                f"{hook.hook_id} and {markers[hook.marker]} share the marker "
                f"{hook.marker!r}. A marker is a per-hook idempotence stamp: sharing one "
                "makes each hook report the other's patch as already applied, and both "
                "vanish from the build without any stage failing."
            )
        markers[hook.marker] = hook.hook_id


def assert_instrumented(hooks: Iterable[Hook]) -> None:
    """Every active hook's payload must announce its own execution.

    Enforced at load rather than left to whoever adds the next hook, because the
    thing it protects against is precisely a hook nobody was watching. Four
    separate patches in this project were present and never ran, and each was
    found by a different ad-hoc investigation months later. An uninstrumented
    hook is one that can fail that way silently, so the manifest refuses it.
    """
    from .runtime_identity import is_instrumented, probe_call

    for hook in hooks:
        if hook.status != "active":
            continue
        if not is_instrumented(hook.payload, hook.hook_id):
            raise ManifestError(
                f"{hook.hook_id}: payload does not call its runtime identity. Add "
                f"{probe_call(hook.hook_id).strip()!r} so this hook reports when it "
                "executes; without it, a patch that is applied but never reached is "
                "indistinguishable from one that works."
            )


def load_manifest(path: Path) -> list[Hook]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ManifestError("unsupported hook manifest schema")
    hooks = [Hook.from_dict(entry) for entry in data["hooks"]]
    assert_distinct(hooks)
    assert_instrumented(hooks)
    return hooks

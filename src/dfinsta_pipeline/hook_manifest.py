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

Some payload values are not adjacent to the site at all, and no anchor extension
can reach them. The profile settings hook needs an own-profile guard whose model
register is the 4th constructor argument on 430 and the 2nd on 439 — so no single
capture name holds it — and whose self-profile type is named on no line near the
site. Those captures are declared in ``supplied_captures`` and filled by a
*supplier* instead: see :class:`CaptureSupply` and
:mod:`dfinsta_pipeline.capture_supply`. The rule stays "every payload capture is
declared by something": an anchor line, or a named supplier. A capture declared
by neither is still refused, and a capture declared by both is a manifest bug
rather than a merge, because there would be no way to say which value wins.

What this module does NOT do: choose between candidate hosts, judge whether a
literal is really the outgoing request path, or run a supplier. Those need search,
judgement, an index and a decode. This module is the deterministic half — it turns
a fingerprint plus a decode into a concrete, checked operation, or fails. It stays
pure: a supplier hands it a *mapping*, which it validates and merges, and it never
calls one.
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

#: The name half of :data:`CAPTURE`, anchored. A `supplied_captures` entry names a
#: capture that no anchor line writes, so nothing else would ever check the name is
#: spellable — and a name the payload cannot reference is a capture that silently
#: never renders.
CAPTURE_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")

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


def anchor_capture_kinds(lines: Iterable[str]) -> dict[str, str]:
    """Capture name -> declared kind, for one whole anchor.

    :func:`compile_anchor` already computes this and throws it away. A capture
    supplier needs it to tell which of an anchor's bindings are REGISTERS, and
    guessing from the value would be wrong: ``KIND_PATTERNS['member']`` also
    matches ``v0``, so a field called ``v0`` would read as a live register.
    """
    kinds: dict[str, str] = {}
    for line in lines:
        compile_pattern(line, kinds)
    return kinds


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
class SuppliedCapture:
    """One payload capture that no anchor line can bind.

    ``kind`` is validated against :data:`KIND_PATTERNS` and re-checked against the
    *value* a supplier returns, which is the whole reason a supplied capture is not
    simply a free string: a supplier — and one of them is an LLM — must be unable
    to inject smali through a capture. ``reg`` admits ``v0``; it does not admit
    ``v0}, LX/Evil;->go()V  #``.

    ``role`` is what the SUPPLIER calls this value; ``name`` is what the payload
    calls it. They are separate so a manifest can rename a capture without
    touching supplier code, and so two suppliers answering the same question can
    be swapped for one another. It defaults to ``name``.
    """

    name: str
    kind: str
    role: str = ""

    def __post_init__(self) -> None:
        if not CAPTURE_NAME.match(self.name):
            raise ManifestError(
                f"supplied capture name {self.name!r} is not a capture name; it must "
                r"match [a-z_][a-z0-9_]* so the payload can write <name>"
            )
        if self.name in RESERVED_CAPTURE_NAMES:
            raise ManifestError(
                f"supplied capture {self.name!r} is reserved: smali writes constructors "
                "as -><init>(...)V and the pattern engine always reads those literally"
            )
        if self.kind not in KIND_PATTERNS:
            raise ManifestError(
                f"supplied capture {self.name!r} has unknown kind {self.kind!r}; "
                f"one of {sorted(KIND_PATTERNS)}"
            )
        if not self.role:
            object.__setattr__(self, "role", self.name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SuppliedCapture:
        return cls(data["name"], data["kind"], data.get("role", ""))


@dataclass(frozen=True)
class CaptureSupply:
    """One supplier question, and the captures its answer fills.

    Grouped rather than one entry per capture because a supplier answers a
    *question*, not a field: the profile guard's model register and self-profile
    type are two halves of one derivation over one method, and splitting them
    would repeat ``params`` and invite two entries that disagree about it.

    ``suppliers`` is an ordered preference chain, tried left to right, first
    non-decline wins. The deterministic supplier goes first and the agent last —
    but only because the deterministic one is written to DECLINE unless every
    precondition is affirmatively proven. A supplier that guesses would make this
    ordering a liability rather than an optimisation.
    """

    provides: tuple[SuppliedCapture, ...]
    suppliers: tuple[str, ...]
    params: tuple[tuple[str, str], ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not self.provides:
            raise ManifestError("a capture supply must provide at least one capture")
        if not self.suppliers:
            raise ManifestError(
                "a capture supply needs at least one supplier; with none the hook can "
                "never resolve and would report 'awaiting' forever rather than escalating"
            )
        if any(not name.strip() for name in self.suppliers):
            raise ManifestError("supplier names must be non-empty")
        if len(set(self.suppliers)) != len(self.suppliers):
            raise ManifestError(f"supplier chain {list(self.suppliers)} repeats a supplier")
        names = [item.name for item in self.provides]
        if len(set(names)) != len(names):
            raise ManifestError(f"capture supply declares {names} with a duplicate name")
        roles = [item.role for item in self.provides]
        if len(set(roles)) != len(roles):
            # Two captures sharing a role would both take the same supplier value,
            # silently, and only one of them would be the one that was meant.
            raise ManifestError(f"capture supply declares {roles} with a duplicate role")
        keys = [key for key, _ in self.params]
        if len(set(keys)) != len(keys):
            raise ManifestError(f"capture supply params repeat a key: {keys}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.provides)

    @property
    def parameters(self) -> dict[str, str]:
        return dict(self.params)

    def by_role(self) -> dict[str, SuppliedCapture]:
        return {item.role: item for item in self.provides}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureSupply:
        return cls(
            provides=tuple(SuppliedCapture.from_dict(item) for item in data["provides"]),
            suppliers=tuple(data["suppliers"]),
            params=tuple((str(key), str(value)) for key, value in data.get("params", {}).items()),
            note=data.get("note", ""),
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
    # Payload captures no anchor line can bind, and who fills them. See
    # `CaptureSupply`. Empty for every hook whose anchor reaches everything its
    # payload needs, which is six of the seven shipped.
    supplied_captures: tuple[CaptureSupply, ...] = ()
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

        supplied: dict[str, SuppliedCapture] = {}
        for group in self.supplied_captures:
            for item in group.provides:
                if item.name in supplied:
                    raise ManifestError(
                        f"{self.hook_id}: supplied capture <{item.name}> is declared by "
                        "two supply groups; one capture has one supplier chain"
                    )
                if item.name in declared:
                    # NOT a merge. Both would be authoritative and there is no rule
                    # for which wins, so the manifest is wrong rather than ambiguous.
                    # An anchor line cannot even reference a supplied capture without
                    # declaring it — a bare `<name>` on first use is already rejected
                    # for having no kind — so this is the only way the two can meet.
                    raise ManifestError(
                        f"{self.hook_id}: <{item.name}> is captured by an anchor line AND "
                        "declared in supplied_captures. A capture has exactly one source; "
                        "if the anchor really binds it, delete the supplied_captures entry"
                    )
                supplied[item.name] = item

        for line in self.payload:
            for match in CAPTURE.finditer(line):
                name = match.group("name")
                if name not in declared and name not in supplied:
                    raise ManifestError(
                        f"{self.hook_id}: payload uses <{name}> which no anchor line "
                        "captures and no supplied_captures entry declares"
                    )

        # An unused supplied capture is the shape of a dropped safety guard: the
        # manifest declares the machinery for an own-profile check and then renders
        # a payload without one, which is exactly how a version-independent payload
        # once shipped without the guard it was written to carry.
        used = {
            match.group("name")
            for line in self.payload
            for match in CAPTURE.finditer(line)
        }
        unused = sorted(set(supplied) - used)
        if unused:
            raise ManifestError(
                f"{self.hook_id}: supplied capture(s) {unused} are declared but never "
                "used by the payload. A supplier is run to fill a hole in the patch; "
                "declaring one the payload ignores means the hole is still there"
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
            supplied_captures=tuple(
                CaptureSupply.from_dict(item) for item in data.get("supplied_captures", ())
            ),
            requires_proposal=bool(data.get("requires_proposal", False)),
        )

    @property
    def supplied_capture_names(self) -> tuple[str, ...]:
        """Every capture a supplier must fill before this hook's payload can render."""
        return tuple(name for group in self.supplied_captures for name in group.names)


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
    # Non-empty when the anchor matched cleanly but the payload still needs values
    # a supplier has not been asked for yet. Machine-readable on purpose: the
    # Resolve stage has to tell "the site is here, fill in two blanks" apart from
    # "the anchor did not match", and prose in `reason` is not a thing to branch on.
    # `resolved` stays False throughout — an awaiting resolution is never appliable.
    awaiting: tuple[str, ...] = ()

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
    """Non-blank, non-.line, non-comment lines, matching the applier's own view.

    Deliberately keeps a *trailing* comment on a line that is otherwise code, even
    though :func:`resolve_in_source` matches without it. This is the view the
    applier and the verifier each reimplement, and it is what they compare a
    concrete anchor against; narrowing it here and nowhere else would make an
    anchor resolve and then match nothing at apply time. :func:`strip_comment`
    exists to be applied at the point of comparison instead.
    """
    out = []
    for index, raw in enumerate(lines):
        text = raw.strip()
        if text and not text.startswith(".line") and not text.startswith("#"):
            out.append((index, text))
    return out


def strip_comment(line: str) -> str:
    """One significant line with any comment removed — the instruction alone.

    baksmali annotates a constant with the value it would decode to under another
    type, so a resource id comes out as ``const v0, 0x7f134a34    # 1.957818E38f``
    and a wide constant as ``const-wide v4, 0x412e848000000000L    # 1000000.0``.
    That is not rare: 66,169 lines of the 439 decode and 62,135 of 430 end in one.
    Any anchor whose last capture sits on such a line could not match, because a
    compiled pattern ends in ``$`` and ``any`` is ``\\S+`` — which is exactly how
    the 439 action-bar hook came back "anchor pattern did not match".

    Stripping it here rather than writing the anchor around it is the only option
    that holds, because whether the annotation appears at all is a property of the
    *number*: baksmali emits it when the bits happen to spell a float with a short
    decimal form. Of the resource-id constants in one dex directory, 70 of 8,432
    carry one on 439 and 168 of 12,397 on 430. So the same field in the same class
    flips between the two forms when the id changes — the action bar's label was
    the bare ``0x7f134a0e`` on 430 and the annotated ``0x7f134a34`` on 439 — and an
    anchor written against either version silently stops matching on the other.

    The scan is quote-aware because splitting on the first ``#`` is actively
    destructive: 1,152 lines of the same decode carry a ``#`` *inside* a string
    literal, and cutting there turns ``const-string v0, "a#b"`` into
    ``const-string v0, "a``. Since a string is the only place smali puts a ``#``
    that does not start a comment, the first unquoted one always does.
    """
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            # Only a string can contain an escape; outside one a backslash is
            # part of no token, so treating it as inert keeps the scan honest.
            escaped = quoted
        elif character == '"':
            quoted = not quoted
        elif character == "#" and not quoted:
            return line[:index].rstrip()
    return line


@dataclass(frozen=True)
class AnchorHit:
    """One place an anchor pattern matched, and what it bound there.

    ``first_line``/``last_line`` index the RAW ``smali.splitlines()`` list, not the
    ``significant()`` view, because the only caller that wants them — a capture
    supplier — needs to find the enclosing ``.method``, and ``.method`` lines
    survive `significant()` but the offsets do not survive its filtering.
    """

    bindings: Mapping[str, str]
    lines: tuple[str, ...]
    first_line: int
    last_line: int


def find_anchor_hits(hook: Hook, smali: str) -> list[AnchorHit]:
    """Every place the hook's anchor matches in one class body.

    Split out of :func:`resolve_in_source` so a capture supplier locates the site
    with the SAME matcher the resolver uses. A supplier reimplementing the loop
    would eventually disagree with it about which method the payload lands in,
    and would do so silently.
    """
    body = significant(smali.splitlines())
    # Match the instruction, keep the line. An anchor is written to describe code,
    # so a baksmali annotation hanging off the end of it must not decide whether it
    # matches — but the annotation is still part of the line the applier will look
    # for, and the applier finds an anchor by literal string comparison against its
    # own copy of `significant()`. So the pattern is tried against the stripped
    # instruction and the emitted anchor is the line as written.
    code = [strip_comment(text) for _, text in body]
    compiled = compile_anchor(hook.anchor)
    width = len(compiled)
    hits: list[AnchorHit] = []

    for start in range(len(body) - width + 1):
        bindings: dict[str, str] = {}
        ok = True
        for offset, (pattern, names) in enumerate(compiled):
            match = pattern.match(code[start + offset])
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
            window = body[start : start + width]
            hits.append(
                AnchorHit(
                    bindings=bindings,
                    # Emitting the matched lines rather than `render(hook.anchor,
                    # bindings)` cannot widen the match: every line here matched a
                    # fully-anchored `^...$` pattern, so any other line equal to it
                    # would have matched too. The two forms therefore differ only
                    # where a trailing comment exists — and there, only this one is
                    # a string the applier can find.
                    lines=tuple(text for _, text in window),
                    first_line=window[0][0],
                    last_line=window[-1][0],
                )
            )
    return hits


def merge_supplied(
    hook: Hook, bindings: Mapping[str, str], supplied: Mapping[str, str]
) -> dict[str, str]:
    """Fold supplier values into the anchor's bindings, refusing anything unsafe.

    This is the trust boundary for a supplier, and one of the two shipped
    suppliers is an LLM. Everything here is a refusal rather than a repair:

    * a name bound by BOTH the anchor and a supplier — no rule says which wins;
    * a name no ``supplied_captures`` entry declares — a supplier answering a
      question nobody asked, which would render into the payload unchecked;
    * a value that does not fully match its declared :data:`KIND_PATTERNS` entry —
      ``reg`` is ``[vp]\\d+`` and nothing else, so no supplier can smuggle a second
      instruction, a register RANGE, or a comment through a capture;
    * a declared name with no value — an incomplete answer is not a partial patch,
      it is no patch.

    Raises rather than returning a failed Resolution because every one of these is
    a contract violation by the CALLER, not a property of the target. The Resolve
    stage catches it and escalates; nothing silently continues.
    """
    declared = {item.name: item for group in hook.supplied_captures for item in group.provides}
    merged = dict(bindings)
    for name, value in supplied.items():
        if name in bindings:
            raise ManifestError(
                f"{hook.hook_id}: supplied capture <{name}> collides with the anchor "
                f"binding <{name}>={bindings[name]!r}. A capture has one source; a "
                "supplier may not overwrite what the anchor proved"
            )
        item = declared.get(name)
        if item is None:
            raise ManifestError(
                f"{hook.hook_id}: <{name}> was supplied but no supplied_captures entry "
                f"declares it (declared: {sorted(declared) or 'none'})"
            )
        if not re.fullmatch(KIND_PATTERNS[item.kind], value):
            raise ManifestError(
                f"{hook.hook_id}: supplied capture <{name}> is declared {item.kind!r} but "
                f"the value {value!r} does not match {KIND_PATTERNS[item.kind]!r}. A "
                "capture is a typed hole, not a text substitution: without this a "
                "supplier could render arbitrary smali into the payload"
            )
        merged[name] = value
    missing = sorted(set(declared) - set(supplied))
    if missing:
        raise ManifestError(
            f"{hook.hook_id}: supplied capture(s) {missing} have no value. The payload "
            "cannot be rendered without them and rendering it partially would emit a "
            "patch missing exactly the part no anchor could reach"
        )
    return merged


def resolve_in_source(
    hook: Hook,
    descriptor: str,
    smali: str,
    supplied: Mapping[str, str] | None = None,
) -> Resolution:
    """Match the hook's anchor pattern inside one class body.

    *supplied* carries values for the hook's ``supplied_captures``. Passing
    ``None`` for a hook that declares some is not an error — it is the first,
    anchor-only pass, and the result comes back unresolved with
    :attr:`Resolution.awaiting` naming what is still needed, so the caller can run
    a supplier against the site this pass just located.
    """
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

    hits = find_anchor_hits(hook, smali)

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

    hit = hits[0]
    concrete_anchor = list(hit.lines)
    awaiting = hook.supplied_capture_names
    if awaiting and supplied is None:
        # The site is found; the payload is not renderable yet. Reported as
        # unresolved with `awaiting` set rather than as a failure, because the
        # caller's next move is to run a supplier against THIS site — and it needs
        # the anchor bindings and the line span to do that.
        return Resolution(
            hook.hook_id,
            False,
            descriptor=descriptor,
            anchor=concrete_anchor,
            bindings=dict(hit.bindings),
            occurrences=len(hits),
            awaiting=awaiting,
            reason=(
                f"anchor matched exactly once, but the payload needs supplied "
                f"capture(s) {list(awaiting)} that no anchor line can bind"
            ),
        )

    bindings = merge_supplied(hook, hit.bindings, supplied or {})
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

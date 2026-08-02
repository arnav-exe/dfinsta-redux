"""Fill payload captures no anchor line can bind.

Six of the seven shipped hooks resolve from their anchor alone. The seventh,
``install_settings_long_click``, has a unique anchor and still cannot render,
because its own-profile guard needs two values the anchor provably cannot bind:

* **the model register.** The anchor's ``invoke-direct {<l>, <a>, <b>, <c>, <d>},
  <listener>-><init>(I,Obj,Obj,Obj)V`` binds four registers and the model is
  ``<b>`` on 439 and ``<d>`` on 430 — the argument order swapped between
  versions, so no single capture NAME holds it.
* **the self-profile type.** ``LX/0Dxw;`` on 439, ``LX/077N;`` on 430, and it
  appears on no line adjacent to the site, so no anchor extension reaches it.

Render the payload without them and the DFInsta dialog attaches to every
profile's Options, including strangers' — a patch that assembles, statically
verifies, and quietly undoes a device-verified exclusion.

===============================================================================
  A SUPPLIER DECLINES.  IT DOES NOT GUESS.
===============================================================================

The deterministic rule below was derived from 430 and 439 only, and a holdout
says plainly how far that reaches: on 340 and 300 there is no ``ProfileActionBar``
at all, no model-subtype dispatch chain, and no ``instagram_menu_outline_24``
(340 has ``instagram_menu_pano_outline_24``, a different asset). Both of the
rule's keys fail together below 430 because both are consequences of one
architectural rewrite — so **430 and 439 are not two independent confirmations**,
and this is a 430+ rule resting on roughly one data point.

That is why the preference order is the way round it is. The chain tries the
deterministic supplier first, but only because the deterministic supplier is
written to decline unless EVERY precondition is affirmatively proven; whatever it
does not prove falls through to the agent, which is the default in the sense that
matters. Inverting that — trusting the rule and escalating only on an obvious
error — is how a version-independent payload once shipped without its guard.

**Decline is not failure.** A decline is a returned :class:`Supplied` with a
machine-readable ``stage``, an empty ``values`` and the evidence actually
checked: the supplier ran, and its precondition does not hold here. A failure is
an exception: the index is unreadable, the request is malformed, the caller asked
a supplier for roles it does not answer. The first is a finding about the target
that the Resolve stage reports and escalates; the second is a fault in the
tooling that must not be mistaken for one. Nothing in this module returns a value
it could not prove, and nothing swallows an error into a decline that would read
like proof of absence.

The precondition is itself worth more than the values it gates. ``ProfileActionBar``
present is the *selector* telling you which of the two settings hooks a version
can even host: 430 and 439 ship two implementations of the same control and a
MobileConfig flag picks between them, which is why patching one left the 430 hook
runtime-inert. A version with no ``ProfileActionBar`` cannot host this variant at
all, and a supplier that answered anyway would be answering about the wrong hook.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .hook_index import HookIndex, IndexUnusable
from .hook_manifest import (
    KIND_PATTERNS,
    AnchorHit,
    CaptureSupply,
    Hook,
    ManifestError,
    anchor_capture_kinds,
    strip_comment,
)

#: ``instance-of vDST, vSRC, LType;`` — the whole of the dispatch evidence.
INSTANCE_OF = re.compile(
    r"instance-of\s+(?P<dst>[vp]\d+)\s*,\s*(?P<src>[vp]\d+)\s*,\s*(?P<type>\S+)\Z"
)

METHOD_START = ".method"
METHOD_END = ".end method"


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class Supplied:
    """What one supplier returned: values keyed by ROLE, or a stated decline.

    ``stage`` is the machine-readable half of a decline and the reason this class
    exists rather than an ``str | None``. A caller — and a test — must be able to
    tell "this version has no ProfileActionBar" from "the drawable is gone" from
    "two subtypes matched" without parsing prose, because those are three
    different findings and only the last is ambiguity.
    """

    supplier: str
    values: Mapping[str, str] = field(default_factory=dict)
    stage: str = ""
    declined: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.declined and self.values:
            raise ManifestError(
                f"{self.supplier}: a decline carries no values, but {sorted(self.values)} "
                "were returned alongside one. Half an answer is not an answer"
            )
        if self.declined and not self.stage:
            raise ManifestError(
                f"{self.supplier}: a decline must name the stage it stopped at, so the "
                "caller can report WHICH precondition failed rather than 'it did not work'"
            )
        if self.stage and not self.declined:
            raise ManifestError(f"{self.supplier}: stage {self.stage!r} set without a reason")
        if not self.declined and not self.values:
            raise ManifestError(
                f"{self.supplier}: returned neither values nor a decline. A supplier that "
                "cannot answer must say so; silence reads as success to the caller"
            )

    @property
    def ok(self) -> bool:
        return not self.declined


def decline(supplier: str, stage: str, reason: str, evidence: Sequence[str] = ()) -> Supplied:
    """A supplier's honest 'my precondition does not hold here'."""
    return Supplied(supplier, {}, stage, reason, tuple(evidence))


@dataclass(frozen=True)
class SupplyOutcome:
    """What a whole preference chain concluded for one supply group.

    ``values`` is keyed by CAPTURE NAME — roles are a supplier-side vocabulary and
    stop here. ``attempts`` keeps every supplier that ran, including the ones that
    declined before the winner, because a gate needs to see that the deterministic
    rule was tried and why it did not apply, not just that an agent answered.
    """

    supply: CaptureSupply
    values: Mapping[str, str] = field(default_factory=dict)
    attempts: tuple[Supplied, ...] = ()
    supplier: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.supplier)

    @property
    def missing(self) -> tuple[str, ...]:
        return () if self.ok else self.supply.names

    def reason(self) -> str:
        """One line a caller can put in front of a human, naming the captures."""
        if self.ok:
            return f"{self.supplier} supplied {sorted(self.values)}"
        detail = (
            "; ".join(f"{item.supplier} declined ({item.stage}): {item.declined}" for item in self.attempts)
            or "no supplier ran"
        )
        return f"supplied capture(s) {list(self.supply.names)} were not produced — {detail}"


@dataclass(frozen=True)
class SupplyRequest:
    """Everything a supplier may look at. Assembled by the Resolve stage.

    The anchor hit is included and not merely the class, because a supplier's job
    is to derive a value *for this site*: scoping the derivation to the enclosing
    method is what makes "the register every instance-of tests" a statement about
    the settings site rather than about the file.
    """

    hook: Hook
    supply: CaptureSupply
    descriptor: str
    smali_path: str
    smali: str
    hit: AnchorHit
    index: HookIndex
    decode: Path
    #: Role -> value, from a validated k-of-n proposal. The agent supplier's input.
    proposed: Mapping[str, str] = field(default_factory=dict)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(item.role for item in self.supply.provides)

    def param(self, key: str) -> str | None:
        return self.supply.parameters.get(key)

    def method_lines(self) -> tuple[int, int]:
        """Raw line span of the ``.method`` the anchor matched inside, inclusive.

        Raises when the hit is not inside one: a smali class body has no
        instructions outside a method, so that is a corrupt request rather than a
        property of the target, and it must not read as a decline.
        """
        lines = self.smali.splitlines()
        start = None
        for index in range(min(self.hit.first_line, len(lines) - 1), -1, -1):
            text = lines[index].strip()
            if text.startswith(METHOD_END):
                break
            if text.startswith(METHOD_START):
                start = index
                break
        if start is None:
            raise ManifestError(
                f"{self.hook.hook_id}: the anchor hit at line {self.hit.first_line + 1} of "
                f"{self.smali_path} is not inside a .method. The request is malformed"
            )
        for index in range(self.hit.last_line, len(lines)):
            if lines[index].strip() == METHOD_END:
                return start, index
        raise ManifestError(
            f"{self.hook.hook_id}: the method containing the anchor in {self.smali_path} "
            "never ends; the decode is truncated"
        )

    def anchored_registers(self) -> set[str]:
        """Register values the anchor actually bound at this site.

        A supplied register has to be live where the payload renders. The anchor
        is the only evidence of that available here — every one of these registers
        was read or written by a matched instruction — so a candidate register that
        is not among them is refused rather than trusted.
        """
        kinds = anchor_capture_kinds(self.hook.anchor)
        return {
            value
            for name, value in self.hit.bindings.items()
            if kinds.get(name) == "reg"
        }


Supplier = Callable[[SupplyRequest], Supplied]


# --------------------------------------------------------- deterministic supplier

PROFILE_GUARD = "profile_action_bar_self_guard"

#: Roles this supplier answers. Asked for anything else it raises rather than
#: declining: a caller wiring it to the wrong question is a bug, not a version
#: whose architecture moved.
PROFILE_GUARD_ROLES = ("model_register", "self_profile_type")

#: Machine-readable decline stages. Tests key on these, not on prose, so that
#: removing a check changes the OUTCOME of a test rather than only its message.
STAGE_MISSING_PARAM = "missing_param"
STAGE_PRECONDITION_TYPE_ABSENT = "precondition_type_absent"
STAGE_DRAWABLE_NOT_INDEXED = "drawable_not_indexed"
STAGE_DRAWABLE_ABSENT = "drawable_absent"
STAGE_NO_DISPATCH_REGISTER = "no_dispatch_register"
STAGE_AMBIGUOUS_DISPATCH_REGISTER = "ambiguous_dispatch_register"
STAGE_DISPATCH_REGISTER_NOT_ANCHORED = "dispatch_register_not_anchored"
STAGE_CANDIDATE_UNREADABLE = "candidate_unreadable"
STAGE_NO_SELF_PROFILE_TYPE = "no_self_profile_type"
STAGE_AMBIGUOUS_SELF_PROFILE_TYPE = "ambiguous_self_profile_type"

#: Minimum distinct types tested against one register before it counts as a
#: dispatch chain. Two, because one `instance-of` is a plain type check —
#: `instance-of v0, v1, Ljava/util/Collection;` sits in this very method on both
#: 430 and 439 — while a self/other model union is tested against its whole
#: subtype set. Measured: 430 tests 11 distinct types against p3, 439 tests 10
#: against p4, and in neither is any other register tested more than once.
DISPATCH_MINIMUM = 2


def _hex_token(value: str) -> re.Pattern[str]:
    """Match a resource id only as a whole token, never inside a longer one."""
    return re.compile(
        rf"(?<![0-9A-Za-z_]){re.escape(value)}(?![0-9A-Za-z_])", re.IGNORECASE
    )


def profile_action_bar_self_guard(request: SupplyRequest) -> Supplied:
    """Derive the own-profile guard's two values from the matched method.

    The derivation, and the precondition each step proves:

    1. ``requires_type`` exists in this decode. This is the SELECTOR: no
       ``ProfileActionBar`` means the version cannot host this hook's variant of
       the control, and any value derived here would describe a different design.
    2. ``self_drawable`` resolves to an id in THIS version. The drawable *name* is
       the durable key — 98.8% of names survive a version step and 0.9% of ids do,
       so the id is re-resolved here and never carried.
    3. Exactly one register in the enclosing method is a dispatch chain — tested
       by :data:`DISPATCH_MINIMUM` or more distinct ``instance-of`` types. That
       register holds the action-bar model.
    4. That register is one the anchor bound at this site, so it is live where the
       payload renders.
    5. Exactly one of the tested subtypes is a class that loads the drawable. That
       is the self-profile type.

    Every "exactly one" is checked as uniqueness, not taken as the first hit, and
    every candidate that cannot be READ makes the count unknown and so declines:
    skipping an unreadable candidate would let "exactly one matched" be true only
    because the second match was never looked at.
    """
    unknown = set(request.roles) - set(PROFILE_GUARD_ROLES)
    if unknown:
        raise ManifestError(
            f"{PROFILE_GUARD} answers {list(PROFILE_GUARD_ROLES)}, not {sorted(unknown)}. "
            "A supplier asked the wrong question is a wiring bug; declining would hide it"
        )
    evidence: list[str] = []

    drawable = request.param("self_drawable")
    required_type = request.param("requires_type")
    missing = [
        key
        for key, value in (("self_drawable", drawable), ("requires_type", required_type))
        if not value
    ]
    if missing:
        return decline(
            PROFILE_GUARD,
            STAGE_MISSING_PARAM,
            f"the manifest did not give this supplier {missing}",
            evidence,
        )
    assert drawable is not None and required_type is not None

    # 1. The selector.
    if not request.index.has(required_type):
        return decline(
            PROFILE_GUARD,
            STAGE_PRECONDITION_TYPE_ABSENT,
            f"{required_type} does not exist in this version. This rule describes the "
            "ProfileActionBar design introduced around 430; a version without that class "
            "decides self/other some other way, and deriving a guard from this method "
            "would describe the wrong architecture",
            evidence,
        )
    evidence.append(f"precondition: {required_type} is present")

    # 2. The drawable, by name, re-resolved for this version.
    try:
        drawable_id = request.index.resource_id("drawable", drawable)
    except IndexUnusable as error:
        return decline(
            PROFILE_GUARD,
            STAGE_DRAWABLE_NOT_INDEXED,
            f"this index cannot answer for drawables: {error}",
            evidence,
        )
    if not drawable_id:
        return decline(
            PROFILE_GUARD,
            STAGE_DRAWABLE_ABSENT,
            f"no drawable named {drawable!r} in this version. The name is the durable "
            "key across versions; if it is gone the asset it identified is gone",
            evidence,
        )
    evidence.append(f"drawable {drawable} resolves to {drawable_id} in this version")

    # 3. The dispatch chain.
    first, last = request.method_lines()
    lines = request.smali.splitlines()
    tested: dict[str, list[str]] = {}
    for index in range(first, last + 1):
        match = INSTANCE_OF.match(strip_comment(lines[index].strip()))
        if match:
            tested.setdefault(match.group("src"), []).append(match.group("type"))
    chains = {
        register: sorted(set(types))
        for register, types in tested.items()
        if len(set(types)) >= DISPATCH_MINIMUM
    }
    evidence.append(
        "instance-of by register in the matched method: "
        + ", ".join(f"{reg}={len(set(types))} type(s)" for reg, types in sorted(tested.items()))
    )
    if not chains:
        return decline(
            PROFILE_GUARD,
            STAGE_NO_DISPATCH_REGISTER,
            f"no register in this method is tested against {DISPATCH_MINIMUM} or more "
            "distinct types, so there is no model-subtype dispatch chain to read the "
            "self/other decision out of",
            evidence,
        )
    if len(chains) > 1:
        return decline(
            PROFILE_GUARD,
            STAGE_AMBIGUOUS_DISPATCH_REGISTER,
            f"{len(chains)} registers ({sorted(chains)}) are each tested against several "
            "types; which one holds the action-bar model cannot be decided from this "
            "method alone",
            evidence,
        )
    register, subtypes = next(iter(chains.items()))
    evidence.append(f"dispatch register {register} tested against {len(subtypes)} subtypes")

    # 4. Live at the insertion point, on the anchor's own evidence.
    anchored = request.anchored_registers()
    if register not in anchored:
        return decline(
            PROFILE_GUARD,
            STAGE_DISPATCH_REGISTER_NOT_ANCHORED,
            f"the dispatch register {register} is not one the anchor bound at this site "
            f"({sorted(anchored)}). A guard on a register the site does not use is a "
            "guard on whatever happens to be there",
            evidence,
        )
    evidence.append(f"{register} is bound by the anchor at this site")

    # 5. The one subtype that draws the icon.
    token = _hex_token(drawable_id)
    hits: list[str] = []
    for subtype in subtypes:
        path = request.index.path_for(subtype)
        if path is None:
            # Framework and library types are legitimately not in the decode's
            # class list; they also cannot load an app drawable. Skipping them
            # cannot hide a hit, unlike skipping a class that IS here.
            continue
        source = request.decode / path
        try:
            body = source.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return decline(
                PROFILE_GUARD,
                STAGE_CANDIDATE_UNREADABLE,
                f"{subtype} is in the index at {path} but cannot be read ({error}), so "
                "how many subtypes load the drawable is unknown and uniqueness cannot "
                "be claimed",
                evidence,
            )
        if token.search(body):
            hits.append(subtype)
    evidence.append(
        f"{len(hits)} of {len(subtypes)} subtypes load {drawable} ({drawable_id}): "
        + (", ".join(hits) or "none")
    )
    if not hits:
        return decline(
            PROFILE_GUARD,
            STAGE_NO_SELF_PROFILE_TYPE,
            f"none of the {len(subtypes)} model subtypes loads {drawable!r}, so nothing "
            "here identifies the own-profile model",
            evidence,
        )
    if len(hits) > 1:
        return decline(
            PROFILE_GUARD,
            STAGE_AMBIGUOUS_SELF_PROFILE_TYPE,
            f"{len(hits)} model subtypes load {drawable!r} ({', '.join(hits)}); the "
            "drawable no longer identifies exactly one, so which is the own-profile "
            "model must be established with more evidence rather than by order",
            evidence,
        )

    return Supplied(
        PROFILE_GUARD,
        {"model_register": register, "self_profile_type": hits[0]},
        evidence=tuple(evidence),
    )


# ----------------------------------------------------------------- agent supplier

AGENT = "agent"

STAGE_NO_PROPOSAL = "no_proposal"
STAGE_INCOMPLETE_PROPOSAL = "incomplete_proposal"


def agent_supplier(request: SupplyRequest) -> Supplied:
    """The same values, from a validated k-of-n proposal.

    This is the SEAM, and it is deliberately dumb: it consumes an already-agreed
    answer and does not run a model. What is missing on the other side of it is a
    narrowed "capture" question in :mod:`dfinsta_pipeline.proposer` — a
    ``CAPTURE_SCHEMA`` of ``{role: value}`` plus ``capture_prompt`` / ``parse_capture``
    / ``collect_captures`` — and a ``capture_agreement`` in
    :mod:`dfinsta_pipeline.proposals` that agrees k-of-n on the VALUES, following
    the grain of ``HOST_SCHEMA`` / ``host_agreement`` rather than inventing a
    third proposal style. Nothing here trusts the answer: it still passes through
    :func:`~dfinsta_pipeline.hook_manifest.merge_supplied`, which kind-checks every
    value, so an agent cannot render smali through a capture even if it tries.
    """
    wanted = set(request.roles)
    if not request.proposed:
        return decline(
            AGENT,
            STAGE_NO_PROPOSAL,
            f"no validated capture proposal was supplied for role(s) {sorted(wanted)}",
        )
    have = {role: value for role, value in request.proposed.items() if role in wanted}
    absent = sorted(wanted - set(have))
    if absent:
        return decline(
            AGENT,
            STAGE_INCOMPLETE_PROPOSAL,
            f"the proposal answers {sorted(have)} but not {absent}; a partial answer "
            "renders a partial patch, which is the failure this hook already had",
        )
    return Supplied(AGENT, have, evidence=("from a validated capture proposal",))


# ----------------------------------------------------------------------- registry

REGISTRY: Mapping[str, Supplier] = {
    PROFILE_GUARD: profile_action_bar_self_guard,
    AGENT: agent_supplier,
}

STAGE_UNKNOWN_SUPPLIER = "unknown_supplier"
STAGE_MALFORMED_VALUE = "malformed_value"
STAGE_INCOMPLETE_ANSWER = "incomplete_answer"
STAGE_UNASKED_ROLE = "unasked_role"


def run_supply_chain(
    request: SupplyRequest, registry: Mapping[str, Supplier] | None = None
) -> SupplyOutcome:
    """Try each supplier in the manifest's order; first non-decline wins.

    A winner's answer is re-checked here before it becomes the outcome: it must
    cover every requested role, and every value must fully match its capture's
    declared kind. A supplier that fails either is recorded as having declined and
    the chain moves on — so a broken deterministic rule falls through to the agent
    instead of poisoning the payload, and the decline is visible in ``attempts``
    rather than absorbed. :func:`~dfinsta_pipeline.hook_manifest.merge_supplied`
    checks the same things again at the render boundary; neither layer relies on
    the other having done it.
    """
    table = REGISTRY if registry is None else registry
    by_role = request.supply.by_role()
    attempts: list[Supplied] = []
    for name in request.supply.suppliers:
        supplier = table.get(name)
        if supplier is None:
            attempts.append(
                decline(
                    name,
                    STAGE_UNKNOWN_SUPPLIER,
                    f"no supplier named {name!r} is registered "
                    f"(registered: {sorted(table) or 'none'})",
                )
            )
            continue
        result = supplier(request)
        if not result.ok:
            attempts.append(result)
            continue
        absent = sorted(set(by_role) - set(result.values))
        if absent:
            attempts.append(
                decline(
                    name,
                    STAGE_INCOMPLETE_ANSWER,
                    f"answered {sorted(result.values)} but not role(s) {absent}",
                    result.evidence,
                )
            )
            continue
        extra = sorted(set(result.values) - set(by_role))
        if extra:
            # A supplier answering more than it was asked has no capture to put the
            # surplus in. Dropping it silently would accept an answer nobody can
            # check; the manifest is the authority on what this group provides.
            attempts.append(
                decline(
                    name,
                    STAGE_UNASKED_ROLE,
                    f"answered role(s) {extra} that this supply group does not declare "
                    f"(declared: {sorted(by_role)})",
                    result.evidence,
                )
            )
            continue
        bad = [
            f"{role}={value!r} is not {by_role[role].kind}"
            for role, value in result.values.items()
            if not re.fullmatch(KIND_PATTERNS[by_role[role].kind], value)
        ]
        if bad:
            attempts.append(
                decline(
                    name,
                    STAGE_MALFORMED_VALUE,
                    "returned a value of the wrong kind: " + "; ".join(bad),
                    result.evidence,
                )
            )
            continue
        attempts.append(result)
        return SupplyOutcome(
            request.supply,
            {by_role[role].name: value for role, value in result.values.items()},
            tuple(attempts),
            name,
        )
    return SupplyOutcome(request.supply, {}, tuple(attempts))

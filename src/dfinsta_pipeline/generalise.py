"""Stage 10, the generalising half: turn one agent finding into a durable rule — or refuse.

`agent_cost` measures the number the project judges itself by, and 439 measured
**2**: the two `ui`-tier hooks whose host fingerprint is ``kind: "by_agent"``.
Nothing writes anything back, so 440 costs the same 2 and the verdict reads FLAT
forever. This module is the write-back.

The route came out of the run itself. The proposers did not merely name a class;
they cited durable evidence, and it is sitting in `discovery.json` under each
proposal's ``evidence`` — a source string that names the class's own former
identity, which is the same fingerprint material `co_literals` already uses for
the Reels hooks. It was measured, printed, and thrown away.

**The verification is the module.** Proposing a fingerprint is one line; proving
it is not a coincidence is everything else:

  * **It must select the class the agent found, in the decode it was found in.**
    The set of classes containing the literal must be exactly the agreed host. If
    it selects two it is not a fingerprint, and an intersection is tried before
    giving up — which is how the Reels hooks work, because `clips/discover/`
    alone appears in 5 classes and only one carries all three endpoints.

  * **It must select correctly on a DIFFERENT version.** This is the check that
    actually bites, and it is not a formality: the systrace string
    ``ProfileActionBarViewBinder.bindUsernameTitle…`` selects exactly one class on
    439 and it is the right one — and exactly one class on 430 and it is the
    WRONG one (`Lcom/instagram/profile/actionbar/ProfileActionBar;`, not the 430
    host `LX/077K;`). Promoting it on the 439 measurement alone would have
    poisoned the next port with something every single-version check called
    perfect. So a proposal needs at least one corroborating version whose host is
    known, and a run with no corroborator produces no fingerprint at all rather
    than an unverified one.

  * **A forbidden value is never proposed.** Not an obfuscated descriptor, not a
    register, not a member name, not a resource id — `decisions.FORBIDDEN_SIGNAL`
    exists because every 430 host name still exists in 439 naming a different
    class, and 103 of 11,737 drawable ids survived 430->439 against 98.8% of the
    names. Two independent guards: :func:`forbidden_reason` refuses by shape, and
    every candidate must additionally have been *observed as a string constant
    inside the host class in the decode*, which no descriptor, register, member
    or id ever is.

**It proposes; it does not commit.** The output is a file for a human to read,
and :func:`write_proposals` refuses to write over a hook manifest. Promoting a
host from `by_agent` to `by_literal` on the strength of one run is exactly the
confident-and-wrong failure this project keeps paying for; the verification above
is what makes the proposal worth reading, and a human still decides.

**"No fingerprint found" is a result, not a failure.** It is the honest answer
for `install_settings_long_click`, whose 430 and 439 hosts share exactly one
string constant (``"Threads"``, 6 classes in each version), and an invented
fingerprint is far worse than an honest "this one still needs an agent".

**What a proposal cannot do by itself**, and says so in :attr:`Proposal.blocks`:
`resolve.search_hosts` resolves `by_literal` through
`HookIndex.descriptors_with_literal`, which holds only API-path-shaped strings —
so a verified literal that is not API-path shaped resolves to *nothing* and the
hook escalates anyway. A proposal that would not actually retire the agent must
say that out loud, or stage 10 becomes a place where the number falls without
anything having been learned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .hook_index import HookIndex
from .hook_manifest import Hook
from .manifest_update import RESOURCE_ID

SCHEMA_VERSION = 1


#: What this stage may propose. `by_anchor` is here because it is the only kind
#: that has ever moved the agent-invocation count: the 2 -> 0 fall between
#: Instagram 439 and 440 came from two `by_anchor` entries a human hand-wrote
#: after measuring that each anchor selects exactly one class per decode. A
#: write-back path that could express every kind except that one could automate
#: only the promotions that have never mattered.
KINDS = frozenset({"", "by_literal", "by_anchor"})


class GeneraliseError(ValueError):
    """Raised when a caller hands this module something it must not act on."""


# ------------------------------------------------------------------ the refusals

#: A smali register. `hook_manifest.KIND_PATTERNS['reg']`, anchored — the same
#: shape, spelled here so a refusal never depends on the pattern engine's table
#: being imported for a different purpose.
REGISTER = re.compile(r"[vp]\d+\Z")

#: A type descriptor, primitive or reference or array.
TYPE_DESCRIPTOR = re.compile(r"\[*(?:L[^;\s]+;|[ZBCSIJFD])\Z")

#: A field or method name as the obfuscator emits them: `A00`, `A0K`, `BHV`.
#: Three characters, leading capital. The one refusal that is about *meaning*
#: rather than length — a member name is what moved between 430 and 439 and it is
#: never identity.
OBFUSCATED_MEMBER = re.compile(r"[A-Z][0-9A-Za-z]{2}\Z")

#: Any hex constant. `RESOURCE_ID` catches the `0x7f……` application resources;
#: this catches the rest, including MobileConfig ids like `0x81099a000034a6`.
HEX_CONSTANT = re.compile(r"0[xX][0-9a-fA-F]+\Z")

#: Below this a literal cannot discriminate anything, and triage is cheaper than
#: a decode scan. Set at 3 rather than 4 deliberately: `A03` is exactly 3 long, so
#: it survives this rule and is refused by :data:`OBFUSCATED_MEMBER` instead —
#: which keeps both rules load-bearing rather than making one shadow the other.
MIN_LITERAL_LENGTH = 3

#: Primary plus three co-literals. The Reels precedent is three; a fourth is
#: room, and beyond that an intersection is describing one version's class rather
#: than the hook's host.
MAX_LITERALS = 4


def forbidden_reason(value: object) -> str:
    """Why *value* may never be a fingerprint, or ``""`` if it may.

    The shape half of the guard. The provenance half — a candidate must have been
    observed as a string constant inside the host class — is enforced by
    :func:`candidate_literals`, and neither is redundant: shape refuses a value a
    caller *suggests*, provenance refuses one the decode does not actually carry.
    """
    if not isinstance(value, str):
        return f"a fingerprint is a string, not a {type(value).__name__}"
    if not value.strip():
        return "an empty literal selects every class in the decode"
    if RESOURCE_ID.search(value):
        return (
            f"{value!r} contains an application resource id. Of 11,737 drawable names "
            "present in both 430 and 439, 103 kept their id — 0.9%. Anchor on the name "
            "and re-resolve the id per version"
        )
    if HEX_CONSTANT.fullmatch(value):
        return f"{value!r} is a hex constant, which is a value this version happens to use"
    if REGISTER.fullmatch(value):
        return (
            f"{value!r} is a register. 430->439 moved set_app_context's register from v0 "
            "to v4 without changing anything else; registers are observations"
        )
    if TYPE_DESCRIPTOR.fullmatch(value):
        return (
            f"{value!r} is a type descriptor. Every 430 host name still exists in 439 "
            "and names a different class, so a descriptor is a join key that returns the "
            "wrong class rather than a miss"
        )
    if "->" in value:
        return f"{value!r} is a member reference; the member name moved between versions"
    if OBFUSCATED_MEMBER.fullmatch(value):
        return f"{value!r} is an obfuscated member name, not a string the app carries"
    if len(value) < MIN_LITERAL_LENGTH:
        return f"{value!r} is too short to discriminate between classes"
    return ""


# -------------------------------------------------------------- reading a decode

#: One string constant, quoted body captured raw. Raw rather than unescaped on
#: purpose: candidates are extracted from this same source, so comparing the text
#: as written keeps both sides in one representation and no escape rule has to be
#: agreed with baksmali.
CONST_STRING = re.compile(
    r'^[ \t]*const-string(?:/jumbo)?[ \t]+[vp]\d+,[ \t]*"((?:[^"\\]|\\.)*)"[ \t]*(?:#.*)?$'
)

CLASS_DECLARATION = re.compile(rb"^\.class\b[^\n]*?(\S+;)[ \t]*$", re.M)


class DecodeLiterals:
    """String constants in one decode, scanned once per batch of questions.

    Deliberately NOT `HookIndex`. The index holds only strings that look like API
    paths — `looks_like_api_path` rejects anything with an uppercase letter or no
    slash — and every literal either settings host carries is one of those it
    rejects. An index lookup would answer "no class has this" for a string sitting
    181,421 files away, which is the one answer a verification must never accept.

    A full pass over the 439 decode is ~4 s and answers every pending literal at
    once, so :meth:`prime` before a batch and the cost is paid once. A decode is a
    read-only artifact, which is why :func:`scanner_for` may cache these
    module-wide.
    """

    def __init__(self, decode: Path | str):
        self.decode = Path(decode)
        if not self.decode.is_dir():
            raise GeneraliseError(f"decode {self.decode} is not a directory")
        self._found: dict[str, tuple[str, ...]] = {}
        self._pending: set[str] = set()
        #: How many full passes this instance has made. Not decoration: a batch
        #: that answers in one pass and a loop that pays for one pass per question
        #: are the difference between a usable stage and an unusable one, and a
        #: test can only hold that line if the number is visible.
        self.passes = 0

    # ------------------------------------------------------------------- one file

    def _read(self, relative: str) -> str:
        path = self.decode / relative
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise GeneraliseError(f"cannot read {path}: {error}") from error

    def descriptor_at(self, relative: str) -> str:
        """The class one smali file declares, read from the file rather than assumed."""
        match = CLASS_DECLARATION.search(self._read(relative).encode("utf-8", "replace"))
        if match is None:
            raise GeneraliseError(f"{relative} declares no class")
        return match.group(1).decode("utf-8")

    def literals_in_file(self, relative: str) -> tuple[str, ...]:
        """Every string constant one class carries, in source order, de-duplicated."""
        out: list[str] = []
        for line in self._read(relative).splitlines():
            match = CONST_STRING.match(line)
            if match is not None and match.group(1) not in out:
                out.append(match.group(1))
        return tuple(out)

    # ------------------------------------------------------------- the whole tree

    def prime(self, literals: Iterable[str]) -> None:
        """Queue literals so the next question answers all of them in one pass."""
        self._pending.update(literal for literal in literals if literal not in self._found)

    def resolve(self) -> None:
        """Answer everything queued. One pass, or none when nothing is queued."""
        wanted = sorted(self._pending)
        if not wanted:
            return
        # Two stages. A quoted-substring test over raw bytes is what makes one pass
        # over 181,421 files affordable; the line match on the few files it hits is
        # what makes the answer exact, so an annotation or a comment that happens to
        # contain the text is never counted as a class carrying the constant.
        needles = {literal: f'"{literal}"'.encode("utf-8") for literal in wanted}
        found: dict[str, list[str]] = {literal: [] for literal in wanted}
        for root, _, names in os.walk(self.decode):
            for name in names:
                if not name.endswith(".smali"):
                    continue
                try:
                    with open(os.path.join(root, name), "rb") as handle:
                        data = handle.read()
                except OSError:  # pragma: no cover - a file that vanished mid-scan
                    continue
                candidates = [
                    literal for literal, needle in needles.items() if needle in data
                ]
                if not candidates:
                    continue
                text = data.decode("utf-8", "replace")
                carried = {
                    match.group(1)
                    for match in (CONST_STRING.match(line) for line in text.splitlines())
                    if match is not None
                }
                confirmed = [literal for literal in candidates if literal in carried]
                if not confirmed:
                    continue
                declaration = CLASS_DECLARATION.search(data)
                if declaration is None:  # pragma: no cover - smali without a .class
                    continue
                descriptor = declaration.group(1).decode("utf-8")
                for literal in confirmed:
                    found[literal].append(descriptor)
        for literal in wanted:
            self._found[literal] = tuple(sorted(found[literal]))
        self._pending.clear()
        self.passes += 1

    def classes_with(self, literal: str) -> tuple[str, ...]:
        """Every class in this decode whose body carries *literal* as a string constant."""
        if literal not in self._found:
            self.prime([literal])
            self.resolve()
        return self._found[literal]

    def classes_with_all(self, literals: Sequence[str]) -> tuple[str, ...]:
        """The intersection — what a `co_literals` fingerprint actually selects."""
        if not literals:
            return ()
        self.prime(literals)
        self.resolve()
        common: set[str] | None = None
        for literal in literals:
            bucket = set(self._found[literal])
            common = bucket if common is None else common & bucket
            if not common:
                return ()
        return tuple(sorted(common or ()))


#: Cached because a decode is an immutable artifact and a full pass costs seconds.
#: Keyed by the resolved path, so two callers naming one decode differently share
#: the scan rather than paying twice for the same answer.
_SCANNERS: dict[str, DecodeLiterals] = {}


def scanner_for(decode: Path | str) -> DecodeLiterals:
    key = str(Path(decode).resolve())
    if key not in _SCANNERS:
        _SCANNERS[key] = DecodeLiterals(decode)
    return _SCANNERS[key]


Scanner = Callable[[Path | str], DecodeLiterals]


# ------------------------------------------------------------------- the subjects


@dataclass(frozen=True)
class KnownHost:
    """Which class a hook lives in, in ONE version, and where that class is on disk.

    A descriptor stamped with the version it belongs to, exactly like
    `decisions.RecalledDescriptor`: it is never a lookup key and never crosses a
    version boundary. Its only use here is as an *expectation to check* — the
    class a candidate fingerprint has to select in that decode, and the reason a
    fingerprint that selects a different one is rejected instead of promoted.
    """

    version: str
    decode: Path
    descriptor: str
    smali_path: str

    def __post_init__(self) -> None:
        for label, value in (
            ("version", self.version),
            ("descriptor", self.descriptor),
            ("smali_path", self.smali_path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise GeneraliseError(f"a known host needs a non-empty {label}")
        if self.smali_path.startswith("/"):
            raise GeneraliseError(
                f"smali_path {self.smali_path!r} is absolute; cite it relative to the decode"
            )
        object.__setattr__(self, "decode", Path(self.decode))

    @classmethod
    def from_dict(cls, version: str, decode: Path | str, data: Mapping[str, Any]) -> KnownHost:
        return cls(version, Path(decode), data["descriptor"], data["smali_path"])


# ---------------------------------------------------------------- what it selects

VERDICT_EXACT = "exact"
VERDICT_EMPTY = "selects_nothing"
VERDICT_AMBIGUOUS = "selects_several"
VERDICT_WRONG = "selects_the_wrong_class"

#: How many selected classes a record keeps. A literal like ``"Required value was
#: null."`` selects 2,825 classes on 439; storing them would bury the one number
#: that matters in a wall of descriptors nobody may join on anyway.
SAMPLE = 5


@dataclass(frozen=True)
class Selection:
    """What one candidate fingerprint selected in one version, against what it had to.

    Keeps a *sample* of the descriptors and the full count, because a human
    reviewing this has to see WHICH class was picked — "one class, and it was the
    wrong one" and "one class, and it was the right one" are the same number, and
    only the first is a reason to refuse. They are version-stamped facts here,
    never fingerprint material; :meth:`Proposal.host_entry` cannot emit one.
    """

    version: str
    literals: tuple[str, ...]
    count: int
    sample: tuple[str, ...]
    expected: str

    @classmethod
    def measure(
        cls, version: str, literals: Sequence[str], selected: Sequence[str], expected: str
    ) -> Selection:
        ordered = list(selected)
        # The expectation goes into the sample whenever it was selected, so a
        # 2,000-class result can never look like it missed the host merely because
        # the host sorted past the cut.
        head = [item for item in ordered if item == expected][:1]
        rest = [item for item in ordered if item != expected]
        return cls(
            version=version,
            literals=tuple(literals),
            count=len(ordered),
            sample=tuple((head + rest)[:SAMPLE]),
            expected=expected,
        )

    @property
    def verdict(self) -> str:
        if not self.count:
            return VERDICT_EMPTY
        if self.count > 1:
            return VERDICT_AMBIGUOUS
        return VERDICT_EXACT if self.sample[0] == self.expected else VERDICT_WRONG

    @property
    def exact(self) -> bool:
        return self.verdict == VERDICT_EXACT

    @property
    def reason(self) -> str:
        # A `by_anchor` selection has no literals — the fingerprint is the hook's
        # own anchor — so naming what did the selecting has to fall back to
        # something rather than rendering an empty string into the middle of a
        # sentence a human reads when a promotion is refused.
        listed = ", ".join(repr(literal) for literal in self.literals) or "the anchor"
        if self.verdict == VERDICT_EXACT:
            return (
                f"{self.version}: {listed} selects exactly {self.expected} — the host this "
                "version is known to use"
            )
        if self.verdict == VERDICT_EMPTY:
            return (
                f"{self.version}: {listed} selects no class at all. A fingerprint that "
                "cannot find a host it is known to have there is measuring the version it "
                "was found in, not the hook"
            )
        if self.verdict == VERDICT_AMBIGUOUS:
            shown = ", ".join(self.sample)
            more = f", and {self.count - len(self.sample)} more" if self.count > len(self.sample) else ""
            held = "including" if self.expected in self.sample else "not among the first"
            return (
                f"{self.version}: {listed} selects {self.count} classes ({shown}{more}) — "
                f"{held} the host {self.expected}. A fingerprint selects one"
            )
        return (
            f"{self.version}: {listed} selects exactly one class, {self.sample[0]}, and the "
            f"host here is {self.expected}. One class is not the same as the right class, "
            "and every single-version check passes this"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "literals": list(self.literals),
            "count": self.count,
            "sample": list(self.sample),
            "expected": self.expected,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Rejection:
    """A candidate that was tried and refused, with the measurement that refused it."""

    literals: tuple[str, ...]
    reason: str
    selections: tuple[Selection, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "literals": list(self.literals),
            "reason": self.reason,
            "selections": [item.to_dict() for item in self.selections],
        }


# ------------------------------------------------------------------- the proposal

#: The manifest-file invariants a `by_literal` promotion has to satisfy, and the
#: mechanical route it has to survive. Named after the tests and the function that
#: enforce them, so a human reading a block can go straight to the thing that will
#: refuse the commit rather than discovering it at commit time.
BLOCK_SEMANTIC_DEP = "test_every_by_literal_host_literal_is_declared_as_a_semantic_dep"
BLOCK_ANCHOR_TEXT = "test_every_by_literal_host_literal_also_appears_in_its_anchor"
BLOCK_NOT_INDEXED = "resolve.search_hosts"


@dataclass(frozen=True)
class Proposal:
    """One hook's proposed host fingerprint, or an explicit statement that there is none.

    ``kind`` is ``""`` when nothing durable was found, and :attr:`found` is the
    only thing a caller should branch on. :meth:`host_entry` raises in that state
    rather than emitting an empty fingerprint, so "we could not generalise this"
    cannot turn into a manifest edit by way of a falsy field nobody checked.
    """

    hook_id: str
    kind: str
    literal: str = ""
    co_literals: tuple[str, ...] = ()
    selections: tuple[Selection, ...] = ()
    reason: str = ""
    rejected: tuple[Rejection, ...] = ()
    blocks: tuple[tuple[str, str], ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise GeneraliseError(
                f"{self.hook_id}: this stage proposes one of {', '.join(sorted(k for k in KINDS if k))} "
                f"or none; {self.kind!r} is neither"
            )
        if not self.reason.strip():
            raise GeneraliseError(f"{self.hook_id}: a proposal must say why, either way")
        if not self.kind:
            return
        if self.kind == "by_anchor":
            # There is nothing to scrub: the fingerprint IS the hook's own anchor,
            # which is already in the manifest and is the very text the patch is
            # spliced into. Nothing is carried between versions, which is also why
            # this kind cannot go stale the way a cited literal can.
            if self.literal or self.co_literals:
                raise GeneraliseError(
                    f"{self.hook_id}: a by_anchor fingerprint is the anchor; a literal "
                    "alongside it would be a second, unchecked claim"
                )
            self._require_corroboration()
            return
        for value in (self.literal, *self.co_literals):
            refusal = forbidden_reason(value)
            if refusal:
                raise GeneraliseError(f"{self.hook_id}: {refusal}")
        if len(set(self.literals)) != len(self.literals):
            raise GeneraliseError(f"{self.hook_id}: a co_literal repeats the primary literal")
        self._require_corroboration()

    def _require_corroboration(self) -> None:
        """Two versions, and exact on every one of them.

        The same rule for every kind, because the failure it stops is the same:
        a fingerprint that is perfect on one version and wrong on another. The
        439 systrace literal selected exactly one class there and a *different*
        one on 430, and would have been committed on the first result alone.
        """
        measured = {item.version for item in self.selections}
        if len(measured) < 2:
            raise GeneraliseError(
                f"{self.hook_id}: a fingerprint measured on {sorted(measured) or 'nothing'} "
                "is not corroborated. One version cannot distinguish a fingerprint from a "
                "coincidence — the 439 systrace literal selected one class there and a "
                "different one on 430"
            )
        wrong = [item for item in self.selections if not item.exact]
        if wrong:
            raise GeneraliseError(
                f"{self.hook_id}: proposed despite {wrong[0].reason}"
            )

    @property
    def found(self) -> bool:
        return bool(self.kind)

    @property
    def literals(self) -> tuple[str, ...]:
        return (self.literal, *self.co_literals) if self.kind == "by_literal" else ()

    @property
    def mechanical(self) -> bool:
        """Would committing this actually retire the agent for this hook?

        False whenever anything is blocked — a proposal that reads as promotable
        and then resolves to nothing is how the agent count falls without anything
        having been learned.
        """
        return self.found and not self.blocks

    def host_entry(self) -> dict[str, Any]:
        """The `hosts` entry a human would paste into the manifest."""
        if not self.found:
            raise GeneraliseError(
                f"{self.hook_id}: no durable fingerprint was found, so there is no host "
                f"entry to emit — the hook stays by_agent. {self.reason}"
            )
        if self.kind == "by_anchor":
            return {"kind": "by_anchor", "note": self.note}
        return {
            "kind": "by_literal",
            "literal": self.literal,
            "co_literals": list(self.co_literals),
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "fingerprint_found": self.found,
            "would_be_mechanical": self.mechanical,
            "reason": self.reason,
            "host_entry": self.host_entry() if self.found else None,
            "measured": [item.to_dict() for item in self.selections],
            "rejected": [item.to_dict() for item in self.rejected],
            "blocks": [{"blocked_by": where, "detail": why} for where, why in self.blocks],
        }


# --------------------------------------------------------------------- the search


def candidate_literals(host: KnownHost, scanner: Scanner = scanner_for) -> tuple[str, ...]:
    """The literals this host actually carries, minus everything forbidden.

    This is the provenance guard, and it is what makes the shape guard's job small:
    a descriptor, a register, a member name and a resource id are none of them
    string constants, so nothing a caller can suggest reaches a measurement unless
    the class really carries it.
    """
    reader = scanner(host.decode)
    seen = reader.descriptor_at(host.smali_path)
    if seen != host.descriptor:
        raise GeneraliseError(
            f"{host.smali_path} in the {host.version} decode declares {seen}, not "
            f"{host.descriptor}. Obfuscated names are recycled: a path/descriptor pair "
            "that disagrees is pointing at a different class, not a renamed one"
        )
    return tuple(
        literal
        for literal in reader.literals_in_file(host.smali_path)
        if not forbidden_reason(literal)
    )


def _select(literals: Sequence[str], host: KnownHost, scanner: Scanner) -> Selection:
    return Selection.measure(
        host.version,
        literals,
        scanner(host.decode).classes_with_all(list(literals)),
        host.descriptor,
    )


def _measure(
    literals: Sequence[str],
    subject: KnownHost,
    corroborators: Sequence[KnownHost],
    scanner: Scanner,
) -> tuple[Selection, ...]:
    """One selection per known version, subject first."""
    return tuple(_select(literals, host, scanner) for host in (subject, *corroborators))


def _note(
    subject: KnownHost,
    literals: Sequence[str],
    selections: Sequence[Selection],
    scanner: Scanner,
) -> str:
    reader = scanner(subject.decode)
    alone = ", ".join(
        f"{literal!r} in {len(reader.classes_with(literal))}" for literal in literals
    )
    where = "; ".join(f"{item.version} -> {item.sample[0]}" for item in selections)
    narrowing = (
        f"Co-location is what selects it: alone in {subject.version} those literals hold "
        f"{alone} class(es). "
        if len(literals) > 1
        else f"Alone in {subject.version} that literal holds {alone} class(es). "
    )
    return (
        f"MEASURED by dfinsta_pipeline.generalise across {len(selections)} decode(s): the "
        f"host is the one class carrying {', '.join(repr(item) for item in literals)} "
        f"({where}). {narrowing}"
        "Corroborated on more than one version on purpose: a literal that selects one "
        "class in the version it was found in and a DIFFERENT class in the previous one "
        "passes every single-version check and poisons the next port."
    )


def generalise_host(
    hook_id: str,
    subject: KnownHost,
    corroborators: Sequence[KnownHost] = (),
    *,
    evidence: Sequence[str] = (),
    hints: Sequence[str] = (),
    max_literals: int = MAX_LITERALS,
    scanner: Scanner = scanner_for,
) -> Proposal:
    """Propose a durable fingerprint for one agreed host, or say plainly that there is none.

    *subject* is the host the proposers agreed on, in the decode they were run
    against. *corroborators* are the same hook's host in other versions, and at
    least one is required: with a single version there is no way to tell a
    fingerprint from a coincidence, and this module exists because the 439
    evidence contained one of each.

    *evidence* is what the proposers cited. It only ever breaks a tie between
    equally selective candidates — a literal the agent quoted comes with a
    human-legible reason — and never admits one; ranking by citation would put
    ``'profile'`` first for the action-bar hook, because that word is in almost
    every evidence line and in 269 classes. *hints* is the adversarial channel:
    values a caller suggests, each recorded as a rejection when it is forbidden or
    when the host does not carry it, so a suggestion is refused visibly rather
    than ignored.
    """
    rejected: list[Rejection] = []
    for hint in hints:
        refusal = forbidden_reason(hint)
        if refusal:
            rejected.append(Rejection((str(hint),), f"never a fingerprint: {refusal}"))

    if not corroborators:
        return Proposal(
            hook_id,
            "",
            reason=(
                "no fingerprint proposed: only one version was available to measure on. A "
                "literal that selects exactly one class in the decode it was found in can "
                "still select the wrong class in the previous version — measured, on this "
                "very hook — and one sample cannot tell those apart"
            ),
            rejected=tuple(rejected),
        )

    candidates = candidate_literals(subject, scanner)
    for hint in hints:
        if not forbidden_reason(hint) and hint not in candidates:
            rejected.append(
                Rejection(
                    (hint,),
                    f"the {subject.version} host does not carry {hint!r} as a string "
                    "constant, so it cannot be what identifies that class",
                )
            )
    if not candidates:
        return Proposal(
            hook_id,
            "",
            reason=(
                f"no fingerprint proposed: the {subject.version} host carries no string "
                "constant this stage may use. That is not a failure of the search — the "
                "class has nothing durable in it to key on, and the hook stays by_agent"
            ),
            rejected=tuple(rejected),
        )

    reader = scanner(subject.decode)
    reader.prime(candidates)
    reader.resolve()
    cited = {literal for literal in candidates if any(literal in line for line in evidence)}
    ranked = sorted(
        candidates,
        key=lambda literal: (len(reader.classes_with(literal)), literal not in cited, literal),
    )

    # One literal at a time first. A single-literal fingerprint is the strongest
    # thing available: nothing about it depends on two strings staying in one class.
    # The corroborating decode is only scanned for a literal that already isolates
    # the host here, so a candidate in 2,825 classes costs no cross-version pass.
    for literal in ranked:
        here = _select([literal], subject, scanner)
        if not here.exact:
            rejected.append(Rejection((literal,), here.reason, (here,)))
            continue
        selections = (here, *(_select([literal], host, scanner) for host in corroborators))
        if all(item.exact for item in selections):
            return _accept(hook_id, subject, [literal], selections, rejected, scanner)
        rejected.append(
            Rejection(
                (literal,),
                "; ".join(item.reason for item in selections if not item.exact),
                selections,
            )
        )

    # Then the intersection, as the Reels hooks do. Restricted to literals EVERY
    # known host carries: one the corroborator's host lacks can only empty the
    # intersection there, so trying it would burn a decode pass to re-derive a fact
    # the corroborator's own literal set already states.
    shared = set(candidates)
    for host in corroborators:
        shared &= set(candidate_literals(host, scanner))
    pool = [literal for literal in ranked if literal in shared]
    for host in (subject, *corroborators):
        # One pass per decode for the whole pool. Without this the greedy asks each
        # version one literal at a time and pays for a full walk of 181,421 files
        # per question.
        reader = scanner(host.decode)
        reader.prime(pool)
        reader.resolve()
    combination = _greedy_intersection(pool, subject, corroborators, max_literals, scanner)
    if combination:
        selections = _measure(combination, subject, corroborators, scanner)
        if all(item.exact for item in selections):
            return _accept(hook_id, subject, combination, selections, rejected, scanner)
        rejected.append(
            Rejection(
                tuple(combination),
                "; ".join(item.reason for item in selections if not item.exact),
                selections,
            )
        )

    shared_note = (
        f"the {subject.version} host and the corroborating host(s) share "
        f"{sorted(shared)} as string constants, and no intersection of those isolates "
        "one class"
        if shared
        else "the hosts share no string constant at all across the versions measured"
    )
    return Proposal(
        hook_id,
        "",
        reason=(
            f"no durable fingerprint found: {shared_note}. This hook still needs an agent, "
            "and saying so is the result — an invented fingerprint would resolve to a "
            "confident wrong class on the next version"
        ),
        rejected=_distinct(rejected),
    )


def _distinct(rejected: Sequence[Rejection]) -> tuple[Rejection, ...]:
    """One entry per candidate. A one-literal intersection is the single already tried."""
    seen: dict[tuple[str, ...], Rejection] = {}
    for item in rejected:
        seen.setdefault(item.literals, item)
    return tuple(seen.values())


def _accept(
    hook_id: str,
    subject: KnownHost,
    literals: Sequence[str],
    selections: Sequence[Selection],
    rejected: Sequence[Rejection],
    scanner: Scanner,
) -> Proposal:
    return Proposal(
        hook_id,
        "by_literal",
        literal=literals[0],
        co_literals=tuple(literals[1:]),
        selections=tuple(selections),
        reason=(
            f"{', '.join(repr(item) for item in literals)} selects exactly the known host "
            f"in every one of the {len(selections)} version(s) measured"
        ),
        rejected=_distinct(rejected),
        note=_note(subject, literals, selections, scanner),
    )


def _greedy_intersection(
    pool: Sequence[str],
    subject: KnownHost,
    corroborators: Sequence[KnownHost],
    max_literals: int,
    scanner: Scanner,
) -> list[str]:
    """Narrow toward one class in the WORST version, not the version it was found in.

    Narrowing on the subject alone is the mistake this function was written with
    the first time, and it is the same mistake the whole module is about. On 439
    ``notifications_entry_point_impression`` + ``profile`` isolates the action-bar
    host exactly, so a subject-only search stops there satisfied — and on 430 that
    pair selects two classes. The pair that works on both is
    ``notifications_entry_point_impression`` + ``ig4a-instagram-schema``, and
    nothing about the subject version prefers it.

    So each step takes the literal that minimises the LARGEST candidate set across
    every known version. Every literal here is carried by every host, so each host
    stays in its own intersection throughout and the sets can only shrink; when the
    largest reaches one, every version has isolated its own host. Stops there — a
    further literal that excludes nothing is not extra safety, it is one more
    string a future version can move.
    """
    hosts = (subject, *corroborators)
    buckets = {
        host.version: {
            literal: set(scanner(host.decode).classes_with(literal)) for literal in pool
        }
        for host in hosts
    }
    chosen: list[str] = []
    current: dict[str, set[str]] | None = None
    remaining = list(pool)
    while remaining and len(chosen) < max_literals:
        scored = []
        for literal in remaining:
            merged = {
                version: (
                    buckets[version][literal]
                    if current is None
                    else current[version] & buckets[version][literal]
                )
                for version in buckets
            }
            if current is not None and merged == current:
                continue  # excludes nothing anywhere; one more string to go wrong
            widest = max(len(value) for value in merged.values())
            # Ties are broken by the subject count and then by how broad the literal
            # is ON ITS OWN, so a rarer string wins over a compiler-emitted one that
            # happens to narrow the same way — the same "most selective first" rule
            # the primary literal is chosen by, rather than a second rule. The
            # literal itself is the last tiebreak, so two runs over one pair of
            # decodes choose the same fingerprint.
            alone = max(len(buckets[version][literal]) for version in buckets)
            scored.append((widest, len(merged[subject.version]), alone, literal, merged))
        if not scored:
            break
        widest, _, _, literal, merged = min(scored, key=lambda item: item[:4])
        chosen.append(literal)
        current = merged
        remaining.remove(literal)
        if widest <= 1:
            break
    return chosen


# ------------------------------------------------------------------- the blockers


def manifest_blocks(
    proposal: Proposal, hook: Hook | None = None, index: HookIndex | None = None
) -> tuple[tuple[str, str], ...]:
    """What would refuse this proposal if a human committed it as-is.

    Computed rather than left to be discovered, because all three refusals happen
    somewhere other than where the fingerprint is written: two in the manifest
    suite and one inside `resolve.search_hosts`, which resolves `by_literal`
    through the API-surface index and returns *nothing* for a literal that index
    never held.
    """
    if not proposal.found:
        return ()
    blocks: list[tuple[str, str]] = []
    if hook is not None:
        missing = [item for item in proposal.literals if item not in hook.semantic_deps]
        if missing:
            blocks.append(
                (
                    BLOCK_SEMANTIC_DEP,
                    f"{missing} are not in this hook's semantic_deps. That invariant is "
                    "right for the Reels hooks, where the literal IS the endpoint the hook "
                    "rewrites and losing it means Instagram dropped the feature; here the "
                    "literal identifies the CLASS and the hook's behaviour does not depend "
                    "on it, so filing it as a semantic dependency would report 'we can no "
                    "longer find the class' as 'the feature is gone'"
                )
            )
        anchor = "\n".join(hook.anchor)
        absent = [item for item in proposal.literals if item not in anchor]
        if absent:
            blocks.append(
                (
                    BLOCK_ANCHOR_TEXT,
                    f"{absent} do not appear in this hook's anchor, and cannot: the anchor "
                    "is view-configuration field writes with no string constant in them. "
                    "That invariant encodes 'by_literal means the ANCHORED endpoint', which "
                    "is true of every by_literal hook shipped so far and unsatisfiable for "
                    "a literal that identifies a class. The fix is a fingerprint kind "
                    "meaning 'the class CONTAINING this literal', not a weakened invariant"
                )
            )
    if index is not None:
        unindexed = [item for item in proposal.literals if not index.literal_is_indexed(item)]
        if unindexed:
            blocks.append(
                (
                    BLOCK_NOT_INDEXED,
                    f"{unindexed} are absent from the API-surface index, which holds only "
                    "API-path-shaped strings. `search_hosts` resolves by_literal through "
                    "that index, so committing this today would make the hook resolve to "
                    "ZERO candidates and escalate anyway — the agent count would not move. "
                    "Indexing general string constants, or a decode fallback, is what this "
                    "proposal actually needs"
                )
            )
    return tuple(blocks)


def with_blocks(
    proposal: Proposal, hook: Hook | None = None, index: HookIndex | None = None
) -> Proposal:
    """The same proposal, carrying what would refuse it. Never changes the fingerprint."""
    return Proposal(
        proposal.hook_id,
        proposal.kind,
        literal=proposal.literal,
        co_literals=proposal.co_literals,
        selections=proposal.selections,
        reason=proposal.reason,
        rejected=proposal.rejected,
        blocks=manifest_blocks(proposal, hook, index),
        note=proposal.note,
    )


# ---------------------------------------------------------------------- the input


@dataclass(frozen=True)
class DiscoveredHost:
    """One hook as `discovery.json` recorded it: the agreed class and what was cited."""

    hook_id: str
    descriptor: str
    smali_path: str
    evidence: tuple[str, ...] = ()

    @classmethod
    def read(cls, entry: Mapping[str, Any]) -> DiscoveredHost | None:
        descriptor = entry.get("descriptor")
        if not descriptor:
            return None
        run = entry.get("run") or {}
        agreeing = [
            item for item in run.get("proposals", ()) if item.get("descriptor") == descriptor
        ]
        paths = {item.get("smali_path") for item in agreeing if item.get("smali_path")}
        if len(paths) != 1:
            # Two agreeing proposers naming two paths for one descriptor is a
            # disagreement about the class, not a detail. Nothing here picks one.
            return None
        evidence = tuple(line for item in agreeing for line in item.get("evidence", ()))
        return cls(entry["hook_id"], descriptor, paths.pop(), evidence)


def read_discovery(path: Path | str) -> tuple[DiscoveredHost, ...]:
    """Every hook a discovery run reached an agreed host for. Hooks it did not are skipped."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = [DiscoveredHost.read(entry) for entry in data.get("hooks", ())]
    return tuple(item for item in out if item is not None)


def read_known_hosts(path: Path | str) -> dict[str, list[KnownHost]]:
    """Corroborating versions, as ``{version: {"decode": …, "hooks": {id: {…}}}}``.

    A human states these. They are the one thing this stage cannot derive: knowing
    that the 430 host was `LX/077K;` is exactly the finding a previous port made,
    and re-deriving it here would be asking the question this module answers.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, list[KnownHost]] = {}
    for version, block in data.items():
        decode = block["decode"]
        for hook_id, entry in block.get("hooks", {}).items():
            out.setdefault(hook_id, []).append(KnownHost.from_dict(version, decode, entry))
    return out


# --------------------------------------------------------------------- the output

#: Refused as an output path. This stage proposes; a human commits.
MANIFEST_BASENAME = "hooks.json"


def _is_hook_manifest(path: Path) -> bool:
    if path.name == MANIFEST_BASENAME:
        return True
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "hooks" in data and "schema_version" in data


def write_proposals(path: Path | str, proposals: Sequence[Proposal], version: str) -> Path:
    """Write the proposals for a human to read. Refuses to write over a hook manifest.

    The refusal is this module's central rule expressed as code rather than as a
    docstring: a generaliser that can write the manifest is a generaliser that
    promotes a host on the strength of one run, which is the failure the whole
    verification above exists to prevent.
    """
    path = Path(path)
    if _is_hook_manifest(path):
        raise GeneraliseError(
            f"{path} is a hook manifest. This stage proposes and a human commits: paste "
            "the host_entry in by hand after reading the measurements, or the review that "
            "makes the proposal worth anything never happens"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "generated_by": "dfinsta_pipeline.generalise",
        "committed": False,
        "proposals": [item.to_dict() for item in proposals],
    }
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def render(proposals: Sequence[Proposal]) -> list[str]:
    """The proposals as lines. Pure, so the CLI's output is testable."""
    lines: list[str] = []
    for proposal in proposals:
        lines.append(proposal.hook_id)
        if proposal.found:
            extra = f" + {list(proposal.co_literals)}" if proposal.co_literals else ""
            lines.append(f"  PROPOSED  by_literal {proposal.literal!r}{extra}")
        else:
            lines.append("  NO FINGERPRINT FOUND — the hook stays by_agent")
        lines.append(f"    {proposal.reason}")
        for item in proposal.selections:
            lines.append(f"    [{item.verdict}] {item.reason}")
        for item in proposal.rejected:
            lines.append(f"    rejected {list(item.literals)}: {item.reason}")
        for where, why in proposal.blocks:
            lines.append(f"    BLOCKED by {where}: {why}")
        if proposal.found and not proposal.mechanical:
            lines.append(
                "    This would NOT make the hook mechanical as things stand, so committing "
                "it would not move the agent count."
            )
        lines.append("")
    return lines


# ------------------------------------------------------------------------- the cli


def generalise_anchor(
    hook: Hook,
    hosts: Sequence[KnownHost],
    *,
    scan: Callable[[Hook, Path], Any] | None = None,
) -> Proposal:
    """Propose `by_anchor` for a hook whose own anchor selects exactly its host.

    **This is the kind that has actually moved the number.** The agent count fell
    from 2 on Instagram 439 to 0 on 440 because two `by_anchor` entries were
    hand-written after someone measured that each anchor selects exactly one class
    per decode. Until now this stage could propose `by_literal` and nothing else,
    so the write-back path could automate every promotion except the only one that
    has ever mattered.

    The measurement is the same one `resolve.scan_for_anchor` performs during a
    port, called here rather than reimplemented: a proposal derived by different
    code than the resolver would be a claim about a scan nobody ran.

    Two things make this kind unusually safe, and they are the reason it is worth
    proposing at all. **Nothing is carried between versions** — the pattern is
    re-matched against the target decode on every port, and it is the same text
    the patch is spliced into, so a version where it stops identifying the host is
    a version where it stops identifying the *site*, and the hook escalates rather
    than resolving somewhere wrong. And the anchor is already in the manifest, so
    there is no new value to scrub.

    ``hosts`` must name at least two versions, for the reason every other
    proposal here needs two: one version cannot tell a fingerprint from a
    coincidence.
    """
    from .resolve import scan_for_anchor  # noqa: PLC0415

    run = scan_for_anchor if scan is None else scan
    selections: list[Selection] = []
    for host in hosts:
        result = run(hook, Path(host.decode))
        selections.append(
            Selection.measure(host.version, (), result.matched, host.descriptor)
        )

    ordered = tuple(selections)
    versions = sorted({item.version for item in ordered})
    if len(versions) < 2:
        return Proposal(
            hook_id=hook.hook_id,
            kind="",
            selections=ordered,
            reason=(
                f"the anchor was measured on {', '.join(versions) or 'nothing'}; one "
                "version cannot tell a fingerprint from a coincidence"
            ),
        )
    wrong = [item for item in ordered if not item.exact]
    if wrong:
        return Proposal(
            hook_id=hook.hook_id,
            kind="",
            selections=ordered,
            reason=(
                f"the anchor is not a host fingerprint here: {wrong[0].reason}. It still "
                "identifies the site; it does not identify the class on its own."
            ),
        )
    counts = ", ".join(
        f"{item.version} -> {item.expected} (1 of {item.count} matched)" for item in ordered
    )
    return Proposal(
        hook_id=hook.hook_id,
        kind="by_anchor",
        selections=ordered,
        reason=(
            f"the anchor selects exactly one class on each of {', '.join(versions)}, and it "
            "is the known host every time"
        ),
        note=f"the anchor is the fingerprint: {counts}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--discovery", type=Path, required=True, help="a run's discovery.json")
    parser.add_argument("--decode", type=Path, required=True, help="the decode it ran against")
    parser.add_argument("--version", required=True, help="the version label, e.g. 439")
    parser.add_argument("--known", type=Path, required=True, help="corroborating hosts, as JSON")
    parser.add_argument("--manifest", type=Path, help="hooks.json, to check the invariants")
    parser.add_argument("--index", type=Path, help="this version's index, to check by_literal")
    parser.add_argument("--out", type=Path, required=True, help="where to write the proposals")
    args = parser.parse_args(argv)

    hooks_by_id: dict[str, Hook] = {}
    if args.manifest is not None:
        from .hook_manifest import load_manifest  # noqa: PLC0415

        hooks_by_id = {hook.hook_id: hook for hook in load_manifest(args.manifest)}
    index = HookIndex.for_decode(args.index, args.decode) if args.index else None

    known = read_known_hosts(args.known)
    proposals: list[Proposal] = []
    for found in read_discovery(args.discovery):
        subject = KnownHost(args.version, args.decode, found.descriptor, found.smali_path)
        proposal = generalise_host(
            found.hook_id,
            subject,
            known.get(found.hook_id, []),
            evidence=found.evidence,
        )
        proposals.append(with_blocks(proposal, hooks_by_id.get(found.hook_id), index))

    try:
        written = write_proposals(args.out, proposals, args.version)
    except GeneraliseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for line in render(proposals):
        print(line)
    print(f"written: {written}  (nothing was committed)")
    return 0 if any(item.found for item in proposals) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

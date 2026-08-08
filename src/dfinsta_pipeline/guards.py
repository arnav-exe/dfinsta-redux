"""Generate `throwIfBlocked` from a declaration, instead of hand-writing smali.

===============================================================================
  WHY THIS IS DETERMINISTIC AND NOT AN AGENT
===============================================================================

A url-block guard carries exactly three facts: the path text, whether to match it
by `endsWith` or `contains`, and which preference switches it. Everything around
those three facts is the same eleven instructions every time. That is a template,
not a judgement, and this project has already made this move once — hook
resolution went from agent-proposed to `by_anchor` and the agent count went to
zero and stayed there across three versions.

What an agent would add here is the ability to invent a shape nobody anticipated,
which is precisely what must NOT happen inside a method that fails live network
requests. So this module generates, and where it cannot express something it
**refuses** and says what it could not express. A guard that silently came out
wrong is a request that silently keeps flowing.

===============================================================================
  THE SHAPE, AND THE ONE THAT DID NOT FIT
===============================================================================

Every guard shipped before 2026-08-08 was *one or more paths → ONE toggle*. The
five endpoints ruled that day included `delivery/background_prefetch`, which
prefetches for the feed and for Reels both, and so belongs to two toggles at
once. The template grew an any-of form for it: the literals are tested once and
each toggle gets its own `if-nez … :cond_block`.

That is worth stating plainly, because it is the answer to "can this be generated
deterministically?" — yes, **provided the generator can refuse**. The very first
batch after the template was written contained a rule the template could not
express, and a generator without a refusal path would have had to guess.

===============================================================================
  WHY LABEL NAMES ARE NOT PART OF THE CONTRACT
===============================================================================

The hand-written method calls its labels `:cond_reels_home` and
`:cond_reels_setting`; this module calls them `:cond_rule_2` and
`:cond_rule_2_toggle`. Those are not the same text and they are the same method:
the assembler discards the names entirely and baksmali renumbers them
positionally. So equivalence is asserted over :func:`normalise`, which replaces
every label with its position, and the test that matters proves this generator
reproduces the **device-proved** seven-rule method instruction for instruction.
Comparing the raw text instead would have made a cosmetic rename look like a
behaviour change, which is how a real difference gets lost in the noise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "GuardError",
    "Literal",
    "Rule",
    "MATCHES",
    "BLOCK_MESSAGE",
    "normalise",
    "render_method",
    "rules_from_manifest",
    "apply_to_source",
]


class GuardError(Exception):
    """Raised rather than emitting a guard whose behaviour was guessed."""


#: How a path may be tested, and the smali each form emits. `contains` takes a
#: `CharSequence` and `endsWith` a `String`; getting that argument type wrong
#: assembles fine and throws `NoSuchMethodError` at runtime, which is why it is
#: data here rather than a formatting string at the call site.
MATCHES = {
    "endswith": ("endsWith", "Ljava/lang/String;"),
    "contains": ("contains", "Ljava/lang/CharSequence;"),
}

#: The one message every rule throws. Deliberately not per-rule: Instagram files
#: our IOException into its own `IgFunctionalErrorEvent` as
#: `NETWORK_FAILURE_REASON`, and that event names `logview_group_by`, so the
#: string very likely reaches Meta. One fixed string is a smaller fingerprint
#: than a per-rule vocabulary that would enumerate which rules a client carries.
#: Owner decision, 2026-08-08. Rule-level attribution is done with a throw-away
#: diagnostic build instead, which never ships.
BLOCK_MESSAGE = "Blocked by DFInsta setting"

PREFERENCE_READER = "Lcom/dfinstagram/dfinstagram;->getBoolTrueEz(Ljava/lang/String;)Z"
METHOD_NAME = "throwIfBlocked"


@dataclass(frozen=True)
class Literal:
    """One path test. The match kind is per-literal, not per-rule.

    The shipped Reels rule tests `/api/v1/clips/homecoming/` with `endsWith` and
    `/clips/discover` with `contains`, under one toggle — so a rule-level match
    kind could not reproduce the method this generator has to reproduce.
    """

    text: str
    match: str = "endswith"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise GuardError("a literal must have text; an empty path matches every request")
        if self.match not in MATCHES:
            raise GuardError(
                f"unknown match {self.match!r} for {self.text!r}; "
                f"expected one of {', '.join(sorted(MATCHES))}"
            )


@dataclass(frozen=True)
class Rule:
    """Some paths, and the toggles any one of which blocks them."""

    literals: tuple[Literal, ...]
    toggles: tuple[str, ...]
    #: Free text emitted as a smali comment above the rule. This is where a
    #: reason like "contains because the app's own matcher is an indexOf" lives,
    #: so the rationale survives regeneration instead of being a comment somebody
    #: has to remember to re-add.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.literals:
            raise GuardError("a rule must test at least one path")
        if not self.toggles:
            raise GuardError(
                f"rule for {self.literals[0].text!r} names no toggle. A rule with no toggle "
                "would block unconditionally, ignoring the user's settings — which is a "
                "behaviour nobody asked for and the opposite of what a toggle is."
            )
        for toggle in self.toggles:
            if not toggle.startswith("disable_"):
                raise GuardError(
                    f"{toggle!r} is not a preference key; DFInsta's keys all start "
                    "with 'disable_'"
                )
        if len(set(self.toggles)) != len(self.toggles):
            raise GuardError(f"rule for {self.literals[0].text!r} repeats a toggle")


# ------------------------------------------------------------------- rendering


def _test_lines(literal: Literal, jump: str, target: str) -> list[str]:
    method, argument = MATCHES[literal.match]
    return [
        f'    const-string v1, "{literal.text}"',
        "",
        f"    invoke-virtual {{v0, v1}}, Ljava/lang/String;->{method}({argument})Z",
        "",
        "    move-result v2",
        "",
        f"    {jump} v2, :{target}",
        "",
    ]


def _toggle_lines(toggle: str, block_label: str = "cond_block") -> list[str]:
    return [
        f'    const-string v1, "{toggle}"',
        "",
        f"    invoke-static {{v1}}, {PREFERENCE_READER}",
        "",
        "    move-result v2",
        "",
        f"    if-nez v2, :{block_label}",
        "",
    ]


def _throw_lines(label: str, message: str) -> list[str]:
    return [
        f"    :{label}",
        "    new-instance v0, Ljava/io/IOException;",
        "",
        f'    const-string v1, "{message}"',
        "",
        "    invoke-direct {v0, v1}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V",
        "",
        "    throw v0",
        "",
    ]


def _rule_lines(
    index: int, rule: Rule, entry: str | None, fallthrough: str, block_label: str = "cond_block"
) -> list[str]:
    lines: list[str] = []
    if entry is not None:
        lines.append(f"    :{entry}")
    for line in rule.note.splitlines():
        lines.append(f"    # {line}".rstrip())
    toggle_label = f"cond_rule_{index}_toggle"
    for position, literal in enumerate(rule.literals):
        last = position == len(rule.literals) - 1
        # Every literal but the last short-circuits INTO the toggle check; the
        # last one falls out to the next rule. With one literal there is nothing
        # to short-circuit to, so no label is emitted and none is referenced —
        # an unused label assembles fine but reads as a branch that is missing.
        if last:
            lines += _test_lines(literal, "if-eqz", fallthrough)
        else:
            lines += _test_lines(literal, "if-nez", toggle_label)
    if len(rule.literals) > 1:
        lines.append(f"    :{toggle_label}")
    for toggle in rule.toggles:
        lines += _toggle_lines(toggle, block_label)
    return lines


def slug(rule: Rule) -> str:
    """A short name for a rule, from its first path. Diagnostic builds only."""
    return re.sub(r"[^a-z0-9]+", "_", rule.literals[0].text.lower()).strip("_")


def render_method(rules: Sequence[Rule], *, diagnostic: bool = False) -> str:
    """The complete `throwIfBlocked`, ready to splice into hooks.smali.

    `.locals 3` regardless of how many rules there are: v0 holds the path, v1 the
    literal or key under test and v2 the boolean, and every rule reuses all
    three. This is why adding a guard can never change the register contract, and
    why the count is a constant here rather than something computed.

    `diagnostic=True` gives every rule its own throw carrying its own message, so
    one grep says which rule fired. **This must never ship.** Instagram files our
    IOException into its own `IgFunctionalErrorEvent`, so a per-rule vocabulary
    would tell Meta which rules a modified client carries — owner decision,
    2026-08-08. It exists because "is this guard ever reached?" has no other
    honest answer, and a guard nobody has seen fire is the failure this project
    ships most. Each diagnostic message CONTAINS :data:`BLOCK_MESSAGE`, so such a
    build is a strict superset: every existing grep, probe and canonical count
    keeps working untouched.
    """
    if not rules:
        raise GuardError(
            "no rules: `throwIfBlocked` with no rule would compile, install, and "
            "silently block nothing. If that is genuinely wanted, delete the hook "
            "rather than generating an empty one."
        )
    _refuse_shadowed(rules)

    lines = [
        f".method public static {METHOD_NAME}(Ljava/net/URI;)V",
        "    .locals 3",
        "",
        "    # GENERATED by src/dfinsta_pipeline/guards.py from the url_block_rules",
        "    # in manifest/hooks.json. Do not edit by hand: the next port regenerates it.",
        "",
        "    if-eqz p0, :cond_return",
        "",
        "    invoke-virtual {p0}, Ljava/net/URI;->getPath()Ljava/lang/String;",
        "",
        "    move-result-object v0",
        "",
        "    if-eqz v0, :cond_return",
        "",
    ]
    for index, rule in enumerate(rules):
        entry = None if index == 0 else f"cond_rule_{index}"
        fallthrough = (
            "cond_return" if index == len(rules) - 1 else f"cond_rule_{index + 1}"
        )
        block = f"cond_block_{index}" if diagnostic else "cond_block"
        lines += _rule_lines(index, rule, entry, fallthrough, block)
    lines += ["    :cond_return", "    return-void", ""]
    if diagnostic:
        for index, rule in enumerate(rules):
            lines += _throw_lines(
                f"cond_block_{index}", f"{BLOCK_MESSAGE} [DIAG-{slug(rule)}]"
            )
    else:
        lines += _throw_lines("cond_block", BLOCK_MESSAGE)
    lines.append(".end method")
    return "\n".join(lines) + "\n"


def _refuse_shadowed(rules: Sequence[Rule]) -> None:
    """Refuse when an earlier rule would swallow a later one under a different toggle.

    Order is behaviour here, not presentation: the rules are tested top to bottom
    and the first match throws or falls through, so a broad `contains` above a
    narrow path means the narrow path's toggle is never consulted. `/feed/timeline/`
    tested with `contains` would swallow `/feed/timeline_stream/` exactly this way,
    and the two are under the same toggle today only by luck.

    Same-toggle shadowing is allowed and reported by nothing, because the outcome
    is identical either way — the request blocks when that toggle is on.
    """
    seen: list[tuple[Literal, tuple[str, ...]]] = []
    for rule in rules:
        for literal in rule.literals:
            for earlier, toggles in seen:
                if set(toggles) == set(rule.toggles):
                    continue
                if _swallows(earlier, literal):
                    raise GuardError(
                        f"{earlier.text!r} (match {earlier.match}, toggles "
                        f"{', '.join(earlier_t for earlier_t in toggles)}) is tested before "
                        f"{literal.text!r} (toggles {', '.join(rule.toggles)}) and matches "
                        "every path that one does, so the later rule's toggles would never "
                        "be consulted. Reorder them, or narrow the earlier match."
                    )
            seen.append((literal, rule.toggles))


def _swallows(earlier: Literal, later: Literal) -> bool:
    """Does every path matching `later` also match `earlier`?"""
    if earlier.match == "contains":
        # A substring test matches anything containing it, so it swallows any
        # later literal that contains it anywhere.
        return earlier.text in later.text
    # endsWith only swallows a later endsWith with the same tail.
    return later.match == "endswith" and later.text.endswith(earlier.text)


# ---------------------------------------------------------------- equivalence


_LABEL = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def normalise(method: str) -> tuple[str, ...]:
    """The instruction sequence, with label NAMES replaced by their position.

    Two methods are the same method when their normal forms are equal. Label text
    is discarded by the assembler and renumbered by baksmali, so comparing it
    would report a rename as a behaviour change — and a real change would then be
    one diff line among many.

    Comments and blank lines go too: a comment cannot alter control flow, and
    keeping them would make the rationale text in a rule's `note` part of the
    behavioural contract.
    """
    order: dict[str, int] = {}

    def number(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in order:
            order[name] = len(order)
        return f":L{order[name]}"

    out = []
    for raw in method.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(_LABEL.sub(number, line))
    return tuple(out)


# ------------------------------------------------------------------- manifest


MANIFEST_KEY = "url_block_rules"


def rules_from_manifest(
    manifest_path: Path | str, hook_id: str = "tigon_url_block"
) -> tuple[Rule, ...]:
    """Read the declared rules, refusing anything the template cannot express."""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entry = next(
        (h for h in data.get("hooks", ()) if h.get("hook_id") == hook_id), None
    )
    if entry is None:
        raise GuardError(f"{manifest_path} declares no hook {hook_id!r}")
    declared = entry.get(MANIFEST_KEY)
    if not declared:
        # NOT an empty tuple. `render_method` would refuse it anyway, but the
        # message would be about having no rules rather than about the manifest
        # not declaring any — and the two are different repairs.
        raise GuardError(
            f"{hook_id} in {manifest_path} has no {MANIFEST_KEY!r}, so there is nothing "
            "to generate from. This is not the same as a hook that blocks nothing."
        )
    return tuple(_rule_from(item) for item in declared)


def _rule_from(item: Mapping[str, Any]) -> Rule:
    literals = item.get("literals")
    if not isinstance(literals, list) or not literals:
        raise GuardError(f"rule {item!r} declares no literals")
    return Rule(
        literals=tuple(
            Literal(text=str(l["text"]), match=str(l.get("match", "endswith")))
            for l in literals
        ),
        toggles=tuple(str(t) for t in item.get("toggles") or ()),
        note=str(item.get("note") or ""),
    )


# --------------------------------------------------------------------- source


def apply_to_source(source_path: Path | str, rules: Sequence[Rule]) -> bool:
    """Rewrite `throwIfBlocked` in place. Returns whether anything changed.

    Only that method: `hooks.smali` also holds `replaceReelsEndpoint`, which is a
    different hook and is not generated. Refuses a file with anything other than
    exactly one `throwIfBlocked` rather than replacing the first — two would mean
    silently generating one and leaving the other, which is the shape of every
    half-applied patch this project has shipped.
    """
    path = Path(source_path)
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(rf"^\.method .*{METHOD_NAME}\(", text, re.M)]
    if len(starts) != 1:
        raise GuardError(
            f"{path} declares {len(starts)} methods named {METHOD_NAME}; expected exactly "
            "one. Generating over the first would leave the other untouched and shipping."
        )
    start = starts[0]
    end = text.index(".end method", start) + len(".end method\n")
    generated = render_method(rules)
    if text[start:end] == generated:
        return False
    path.write_text(text[:start] + generated + text[end:], encoding="utf-8")
    return True


def read_method(source_path: Path | str) -> str:
    """The `throwIfBlocked` currently in a source file, for comparison."""
    text = Path(source_path).read_text(encoding="utf-8")
    match = re.search(rf"^\.method .*{METHOD_NAME}\(", text, re.M)
    if match is None:
        raise GuardError(f"{source_path} declares no {METHOD_NAME}")
    end = text.index(".end method", match.start()) + len(".end method\n")
    return text[match.start() : end]


def _cli(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=Path("manifest/hooks.json"))
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("dfinsta_source_439/newCode/com/dfinstagram/hooks.smali"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the source does not already match the declaration, and "
        "change nothing. This is the form a build or a test runs.",
    )
    args = parser.parse_args(argv)
    try:
        rules = rules_from_manifest(args.manifest)
        generated = render_method(rules)
        if args.check:
            current = read_method(args.source)
            if normalise(current) == normalise(generated):
                print(f"{args.source} matches {args.manifest}: {len(rules)} rules")
                return 0
            print(
                f"{args.source} does NOT match the {len(rules)} rules declared in "
                f"{args.manifest}. Run without --check to regenerate.",
                file=sys.stderr,
            )
            return 1
        changed = apply_to_source(args.source, rules)
        print(
            f"{'rewrote' if changed else 'unchanged'} {args.source}: {len(rules)} rules, "
            f"{sum(len(r.literals) for r in rules)} paths"
        )
        return 0
    except (GuardError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())

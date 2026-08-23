"""Does every toggle the guard reads have a settings row that works?

    python -m dfinsta_pipeline.settings_ui [--manifest …] [--custom-code …]

`throwIfBlocked` blocks a request when `getBoolTrueEz(key)` is true, and
`getBoolTrueEz` is `getBoolean(key, true)` — **one hardcoded default, for every
key, in `dfinsta.smali`**. There is no per-key default anywhere in the shipped
tree. So a preference key with no settings row does not default to off. It
defaults to **on**, the endpoint is blocked permanently, and the user has no way
to turn it back on.

Nothing detected that. `guards.Rule` checks only that a toggle starts with
`disable_`; `rulings.existing_preference_keys` reads the keys out of
`throwIfBlocked` itself, so it returns whatever the manifest just declared and is
structurally incapable of noticing a key the dialog has never heard of. A build
with such a key assembles, verifies and ships.

===============================================================================
  THE FAILURE THAT IS QUIET, AND THE ONE THAT IS QUIETER
===============================================================================

The settings dialog is two methods in `SettingsWrapper.smali` and a toggle needs
**both**:

* `onLongClick` builds the dialog — a label in the `CharSequence` array and a
  `getBoolean` into the checked-state array, each at an index.
* `onClick` maps the index the user tapped back to a key and writes it.

A key wired into the first and missed in the second gives a row that renders,
animates, reports itself checked, and **writes nothing** — `onClick`'s chain ends
`if-ne p2, v0, :cond_return` and `:cond_return` is `return-void`. No default
branch, no log.

Quieter still is a **misnumbered** row: label at index 3, dispatch at index 4.
Then tapping one row writes another row's key, and the dialog is not wrong about
anything a reader would look at. That is why this module tracks the index
registers rather than trusting document order — assuming the order is assuming
the thing worth checking.

===============================================================================
  WHAT IT DOES NOT CHECK
===============================================================================

That a label reads well, and that the key means what the label says. `Disable
profile ads` maps to `disable_adds`, and no derivation produces that pairing —
`rulings` refuses to guess a preference key for the same reason. This checks
that the wiring is complete and consistent, never that it is well named.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

__all__ = [
    "SettingsError",
    "SettingsRows",
    "WRAPPER_PATH",
    "read_rows",
    "coverage",
    "check",
    "main",
]

WRAPPER_PATH = Path("newCode") / "com" / "dfinstagram" / "SettingsWrapper.smali"

_METHOD = re.compile(r"^\.method .*?\b(?P<name>\w+)\(", re.M)
_CONST_INT = re.compile(r"^\s*const(?:/4|/16|)\s+(?P<reg>[vp]\d+),\s*(?P<value>-?0x[0-9a-f]+|\d+)\s*$")
_CONST_STR = re.compile(r'^\s*const-string\s+(?P<reg>[vp]\d+),\s*"(?P<text>[^"]*)"\s*$')
_APUT = re.compile(r"^\s*aput-(?P<kind>object|boolean)\s+(?P<src>[vp]\d+),\s*(?P<arr>[vp]\d+),\s*(?P<idx>[vp]\d+)\s*$")
_GETBOOL = re.compile(
    r"^\s*invoke-interface\s+\{(?P<args>[^}]*)\},\s*Landroid/content/SharedPreferences;->getBoolean\("
)
_IF_NEZ = re.compile(r"^\s*if-nez\s+p2,\s*:(?P<label>\S+)\s*$")
_IF_NE = re.compile(r"^\s*if-ne\s+p2,\s*(?P<reg>[vp]\d+),\s*:(?P<label>\S+)\s*$")


class SettingsError(RuntimeError):
    """Raised when the dialog and the guard do not agree about the toggles."""


@dataclass(frozen=True)
class SettingsRows:
    """What the dialog offers, read out of the smali that builds it."""

    #: index -> the text the user reads.
    labels: Mapping[int, str]
    #: index -> the key whose current value that row shows.
    read: Mapping[int, str]
    #: index -> the key that row writes when tapped.
    written: Mapping[int, str]

    @property
    def keys(self) -> tuple[str, ...]:
        """Every key the dialog can both show and write, in index order."""

        return tuple(
            self.read[index]
            for index in sorted(self.read)
            if self.read.get(index) == self.written.get(index)
        )


def _body(source: str, name: str) -> str:
    """One method's body. Refuses rather than returning the wrong method's."""

    starts = [
        match.start() for match in _METHOD.finditer(source) if match.group("name") == name
    ]
    if len(starts) != 1:
        raise SettingsError(
            f"{name} appears {len(starts)} times in the settings wrapper; one is expected, "
            "and reading the wrong copy would report a dialog nobody ships"
        )
    end = source.index(".end method", starts[0])
    return source[starts[0] : end]


def read_rows(source: str) -> SettingsRows:
    """Parse the dialog out of `SettingsWrapper.smali`.

    A small register interpreter, because the indices are held in registers and
    reused: `aput-object v5, v4, v7` says "the string in v5 goes at the index in
    v7", and v7 was `0x2` forty lines earlier. Document order happens to match
    array order today; checking it that way would be checking nothing.
    """

    labels: dict[int, str] = {}
    read: dict[int, str] = {}
    ints: dict[str, int] = {}
    strings: dict[str, str] = {}
    pending_key: str | None = None

    for line in _body(source, "onLongClick").splitlines():
        found = _CONST_INT.match(line)
        if found:
            value = found.group("value")
            ints[found.group("reg")] = int(value, 16) if value.startswith("0x") else int(value)
            strings.pop(found.group("reg"), None)
            continue
        found = _CONST_STR.match(line)
        if found:
            strings[found.group("reg")] = found.group("text")
            ints.pop(found.group("reg"), None)
            continue
        found = _GETBOOL.match(line)
        if found:
            arguments = [item.strip() for item in found.group("args").split(",")]
            # `{prefs, key, default}` — the key is the second.
            pending_key = strings.get(arguments[1]) if len(arguments) > 1 else None
            continue
        found = _APUT.match(line)
        if found:
            index = ints.get(found.group("idx"))
            if index is None:
                raise SettingsError(
                    f"the settings dialog stores at an index this reader cannot follow: "
                    f"{line.strip()!r}. Refusing rather than guessing which row it is"
                )
            if found.group("kind") == "object":
                labels[index] = strings.get(found.group("src"), "")
            elif pending_key is not None:
                read[index] = pending_key
                pending_key = None

    written: dict[int, str] = {}
    index: int | None = None
    for line in _body(source, "onClick").splitlines():
        found = _CONST_INT.match(line)
        if found:
            value = found.group("value")
            ints[found.group("reg")] = int(value, 16) if value.startswith("0x") else int(value)
            continue
        if _IF_NEZ.match(line):
            # `if-nez p2, :next` is the index-zero arm: fall through when p2 == 0.
            index = 0
            continue
        found = _IF_NE.match(line)
        if found:
            index = ints.get(found.group("reg"))
            continue
        found = _CONST_STR.match(line)
        if found and index is not None:
            written[index] = found.group("text")
            index = None
    return SettingsRows(labels=labels, read=read, written=written)


def coverage(
    manifest_path: Path | str = Path("manifest") / "hooks.json",
    custom_code: Path | str = Path("dfinsta_source"),
) -> tuple[SettingsRows, tuple[str, ...]]:
    """The dialog's rows and the toggles the guard actually reads."""

    from .guards import rules_from_manifest, toggles_of  # noqa: PLC0415

    wrapper = Path(custom_code) / WRAPPER_PATH
    try:
        source = wrapper.read_text(encoding="utf-8")
    except OSError as error:
        raise SettingsError(f"{wrapper}: {error}") from error
    return read_rows(source), toggles_of(rules_from_manifest(manifest_path))


def check(
    manifest_path: Path | str = Path("manifest") / "hooks.json",
    custom_code: Path | str = Path("dfinsta_source"),
) -> SettingsRows:
    """Refuse unless every toggle has a row that shows it and writes it.

    Four ways to fail, and each is a different repair:

    * **a toggle with no row at all** — the endpoint is blocked with no way to
      unblock it, because the shared default is `true`;
    * **a row that shows a key and writes another** — tapping one row changes a
      different setting, and nothing on screen says so;
    * **a row that shows a key and writes nothing** — it renders, animates,
      reports itself checked and does nothing;
    * **a row for a key no rule reads** — harmless to the user and a sign the
      manifest and the dialog have drifted, which is how the first one arrives.
    """

    rows, declared = coverage(manifest_path, custom_code)

    crossed = sorted(
        (index, rows.read[index], rows.written.get(index))
        for index in rows.read
        if rows.read[index] != rows.written.get(index)
    )
    if crossed:
        index, shown, writes = crossed[0]
        written = repr(writes) if writes else "nothing"
        raise SettingsError(
            f"settings row {index} shows {shown!r} and writes {written}. A row that "
            "writes another row's key changes a setting the user did not touch; one "
            "that writes nothing renders, animates and reports itself checked while "
            "doing nothing"
        )

    offered = set(rows.keys)
    missing = sorted(set(declared) - offered)
    if missing:
        raise SettingsError(
            f"{missing[0]!r} is read by throwIfBlocked and has no settings row. "
            "`getBoolTrueEz` is `getBoolean(key, true)` for every key — there is no "
            "per-key default — so this endpoint is blocked and cannot be unblocked. "
            f"Add a row to {WRAPPER_PATH}: a label, a getBoolean into the checked "
            "array, and an arm in onClick's dispatch"
        )
    stale = sorted(offered - set(declared))
    if stale:
        raise SettingsError(
            f"the settings dialog offers {stale[0]!r} and no url_block_rule reads it. "
            "The switch does nothing, and a dialog that has drifted from the manifest "
            "is how a key with no row arrives"
        )
    if len(rows.labels) != len(offered):
        raise SettingsError(
            f"the dialog shows {len(rows.labels)} label(s) for {len(offered)} working "
            "row(s). A label with no row behind it is a switch that does nothing"
        )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=Path("manifest") / "hooks.json")
    parser.add_argument("--custom-code", type=Path, default=Path("dfinsta_source"))
    args = parser.parse_args(argv)
    try:
        rows = check(args.manifest, args.custom_code)
    except SettingsError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"{len(rows.keys)} toggle(s), each shown and written by its own row:")
    for index in sorted(rows.read):
        print(f"  {index}  {rows.labels.get(index, '(no label)'):24} {rows.read[index]}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())

"""`reversal.py` is the only way back through two doors that were one-way.

Every other module in this pipeline exists to make a decision expensive and
permanent. This one undoes decisions, so it is the module where being wrong is
cheapest to do and most expensive to notice: an over-eager reversal quietly
un-blocks endpoints nobody re-examined, and a reversal that silently changes
nothing leaves a human believing they fixed a broken build.

The properties pinned here are the ones where being wrong is invisible:

**The join key is `(original_decision_id, subject)`, never the id alone.**
:class:`JoinKeyTests`. One gate decision covers every candidate in its docket —
six rulings shared one `decision_id` on 2026-08-08 — so a reader keyed on the id
would withdraw all six when a human withdrew one, and every one of the other five
would look exactly like a decision that had been made. Both halves are attacked
in one corpus: six endpoints blocked under one decision id, and two hooks retired
under one. Withdrawing one of each must leave the siblings untouched, and the
sibling assertion is made through the *consumers* — the manifest on disk and
`expectation.retirements_in_force` — not only through `reversal.withdrawn`.

**A withdrawal is dated, and the date only points forward.**
:class:`EffectiveFromTests` and :class:`InForceTests`. A retirement withdrawal
must carry `effective_from` and a block withdrawal must not, and the asymmetry is
argued in the module docstring rather than inherited. The version tests span
442-446 around a withdrawal ruled for 446: 442 through 445 must read exactly as
they did before the row existed, and 446 must read as though the retirement were
not there. A withdrawal that reached back would rewrite what an already-assessed
port owed, which is the same failure `retired_by` was written to prevent from the
other direction.

**`retirements_in_force` is the only correct reader, and its callers are checked
one at a time.** :class:`ConsumerTests`. This project has shipped a gate with no
producer, a producer whose rulings had no consumer, and a required evidence kind
nothing emitted, so a reversal store that nothing reads is the most likely way
for this module to be wrong. Three consumers are asserted against a
before-and-after control in one corpus: a withdrawn retirement makes the hook a
retirement candidate again, lets a case be built for it, and turns
`expectation.compare`'s `retired` back into `dropped`. :class:`KnownDefectTests`
1 is the fourth consumer, which does not.

**Only a human signs one.** :class:`SigningTests`. `ruled_by: agent` is refused
in every casing, and every required field is refused blank, whitespace and
absent. The reason is the one `retirement` gives for the same rule: if the thing
being measured could withdraw the measurement, the cheapest route past a red
build is to undo the rule rather than fix the hook.

**The match is the app's match.** :class:`UnblockPlanTests`. `plan_unblock` finds
the manifest entry through `assessment.normalise`, so a decision recorded as
`feed/timeline_stream/` withdraws a manifest spelled `/feed/timeline_stream/` and
a decision recorded as `clips/homecoming/` withdraws `/api/v1/clips/homecoming/`.
Two spellings of one rule is how an entire grouping went invisible on 440, and a
raw-string comparison here fails *closed* in the worst way: the reversal records
cleanly and the endpoint stays blocked.

**The record is written before the manifest.** :class:`ApplyUnblockTests`. Proved
by making the manifest write fail and asserting the reversal is still on disk,
rather than by reading the source and believing it.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

Two kinds of corpus, because this module joins two files that nothing else joins.

The manifests are built by :func:`hook_entry`, which produces an entry
`hook_manifest.load_manifest` accepts — a host fingerprint, an anchor, a payload
carrying the hook's own `probe_call`, and a per-hook marker. `apply_unblock`
writes through `write_manifest_atomically`, which loads the result before it
renames it, so a fixture the loader refuses would make every apply test pass for
a reason that has nothing to do with this module. The endpoints are the real
manifest's, spelled the way the real manifest spells them: `/feed/timeline/` with
a leading slash and `feed/timeline_stream/` without one, in the same list, which
is the condition that makes the normalise tests mean something.

The evidence corpora are `tests/test_retirement`'s, imported rather than copied.
Both modules read the same tree and a fixture that diverged would let this file
agree with a repository layout that cannot exist.

Nothing here writes into the repository's own `manifest/`. Every call passes
`root=`, `path=` or `--root` into a `tempfile` tree, and in particular no test
creates `manifest/reversals.jsonl`, whose absence is the state
`manifest/REVERSALS.md` documents.

===============================================================================
  MUTATION RESULTS
===============================================================================

Thirty-seven mutations were applied one at a time to an out-of-tree copy of the
repository, each to a file restored from a pristine copy with every
`__pycache__` cleared before the run — a mutate-run-restore cycle inside one
second that does not change the file's size is invisible to the bytecode cache,
and the interpreter then measures code that is not on disk. The unmutated copy
was run first as the control and again at the end. The bracketed number is how
many distinct tests in this file failed.

None survived.

Withdrawing more than was withdrawn:

* `reversal.withdrawn` keys on `original_decision_id` alone [16] and drops the
  kind filter [2]; `expectation.retired_by` treats a withdrawal of the decision
  id as a withdrawal of every hook under it [5] → :class:`JoinKeyTests`,
  :class:`InForceTests`, :class:`ConsumerTests`
* `withdrawn_at` ignores the version [6], compares with `<` instead of `<=` [11],
  and drops the numeric guard [1] → :class:`InForceTests`
* `Reversal.__post_init__` allows `effective_from` on a block [1] and stops
  requiring it on a retirement [3] → :class:`EffectiveFromTests`
* `append` drops the duplicate-withdrawal check [3] → :class:`RecordTests`,
  :class:`ApplyUnblockTests`, :class:`CliTests`

Not refusing what should be refused:

* `__post_init__` drops the agent check [3], the schema check [1], the
  unknown-kind check [1], and each of the five required-field checks
  [2, 2, 2, 2, 4] → :class:`SigningTests`, :class:`RecordTests`
* `from_dict` accepts unknown keys [1] and a non-object [2] →
  :class:`RecordTests`
* `read_reversals` skips a malformed line instead of refusing [3] →
  :class:`RecordTests`, :class:`CliTests`
* `apply_unblock` drops the `confirm` guard [3] and the staleness check [2];
  `main`'s retirement branch drops its own `confirm` guard [1] →
  :class:`ApplyUnblockTests`, :class:`CliTests`
* `plan_unblock` succeeds when nothing matched [4] and stops refusing a
  non-canonical manifest [2] → :class:`UnblockPlanTests`, :class:`CliTests`
* `main` returns 0 on a refusal [12] → :class:`CliTests`, :class:`SigningTests`,
  and one `expectedFailure` in :class:`KnownDefectTests` turns into an
  unexpected success, which is the convention working as intended

Reading or writing the wrong thing:

* `plan_unblock` compares raw strings instead of `normalise` [5] →
  :class:`UnblockPlanTests`
* `apply_unblock` writes the manifest before the record [2] →
  :class:`ApplyUnblockTests`
* `retirements_in_force` calls `read_retirements` without withdrawals [11] and
  `retired_by` ignores its `withdrawn` argument [12] → :class:`InForceTests`,
  :class:`ConsumerTests`, :class:`JoinKeyTests`
* `compare` [2], `candidates` [2] and `build_case` [1] read the raw retirement
  file → :class:`ConsumerTests`
* `read_reversals` ignores the `record` wrapper [2]; `to_dict` writes
  `effective_from` when empty [3]; `reversal_id` drops `subject` from the digest
  [2]; `render` prints nothing for an empty store [1] → :class:`RecordTests`,
  :class:`EffectiveFromTests`, :class:`CliTests`

===============================================================================
  KNOWN DEFECTS
===============================================================================

:class:`KnownDefectTests` are `expectedFailure` on purpose — the convention
`tests/test_retirement.py`, `tests/test_expectation.py` and `tests/test_reaper.py`
each used for a defect their own tests found. Each asserts what these modules'
docstrings say must happen and what the code does not do, so the suite stays
green today and reports an *unexpected success* the moment one is closed.

1. `expectation.sweep` — which is what `python -m dfinsta_pipeline.expectation`
   runs when `--version` is omitted, the default mode — reads
   `read_retirements(root)` once and hands it to `compare` as `retirements=`,
   and `compare` then takes the `retired_by(version, retirements)` branch with no
   `withdrawn` argument. So the sweep honours a retirement a human has already
   withdrawn. `retirements_in_force`'s own docstring says "every consumer of 'is
   this hook retired' goes through here", and this one does not: the same corpus
   reports the drop under `--version 446` and hides it under a bare sweep.
2. `reversal.main` leaves a `KeyError` as a traceback. A manifest with no
   `"hooks"` key reaches `document["hooks"]` in `plan_unblock`; `main` catches
   `ReversalError`, `ValueError` and `OSError`, and `KeyError` is none of them,
   so the tool exits 1 with a stack trace rather than `refused:` with exit 2.
   The same shape sits at `entry["hook_id"]`. `ManifestError` is a `ValueError`
   and *is* caught, which is what makes the gap look accidental.
3. `--manifest` defaults to `Path("manifest/hooks.json")`, resolved against the
   process's working directory, while `--reversals` defaults to
   `<root>/manifest/reversals.jsonl`. Run from anywhere but the repository root,
   `withdraw-block --root <repo>` records the reversal inside the repository and
   reads the manifest outside it. One flag scopes half the operation.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from unittest import mock

from dfinsta_pipeline import expectation as expectation_module
from dfinsta_pipeline import reversal as reversal_module
from dfinsta_pipeline.assessment import normalise
from dfinsta_pipeline.expectation import (
    RETIREMENTS,
    Retirement,
    read_retirements,
    retired_by,
    retirements_in_force,
)
from dfinsta_pipeline.manifest_patch import serialise
from dfinsta_pipeline.retirement import RetirementError, build_case, candidates, publish
from dfinsta_pipeline.reversal import (
    KINDS,
    REVERSALS,
    Reversal,
    ReversalError,
    append,
    apply_unblock,
    plan_unblock,
    read_reversals,
    render,
    reversal_id,
    withdrawn,
    withdrawn_at,
)
from dfinsta_pipeline.runtime_identity import probe_call

from tests.test_retirement import (
    CONTEXT,
    DISCOVER,
    TIGON,
    RetirementTestCase,
    triple,
)

# ------------------------------------------------------------------- constants

#: One gate decision id, shared by every ruling in one docket. This is the whole
#: reason the join key is a pair: the real docket on 2026-08-08 carried six
#: rulings under one id, and a reader keyed on the id would treat a withdrawal of
#: any one of them as a withdrawal of all six.
DOCKET = "decision-441-docket"
OTHER_DOCKET = "decision-442-docket"

#: The six endpoints that docket blocked, spelled as `manifest/hooks.json`
#: really spells them — a leading slash on some and not others, and one carrying
#: the `api/v1/` prefix `assessment.normalise` strips. Mixed on purpose: a
#: uniformly spelled fixture would let a raw-string comparison pass.
BLOCKED: tuple[str, ...] = (
    "/feed/timeline/",
    "/discover/topical_explore",
    "/api/v1/clips/homecoming/",
    "feed/reels_media_stream/",
    "feed/timeline_stream/",
    "delivery/background_prefetch",
)

#: The one this file withdraws, and the spelling a decision recorded it under.
#: The manifest holds it WITHOUT a leading slash, so the reversal names it WITH
#: one and the two must still meet.
SUBJECT = "feed/timeline_stream/"
SUBJECT_SLASHED = "/feed/timeline_stream/"

#: The `api/v1/` case, in the other direction: recorded bare, spelled long in the
#: manifest.
PREFIXED_SUBJECT = "clips/homecoming/"
PREFIXED_IN_MANIFEST = "/api/v1/clips/homecoming/"

ARNAV = "arnav"
WHEN = "2026-08-09T10:00:00Z"
RATIONALE = (
    "Broke the feed on the device: an endless spinner rather than an empty feed, "
    "reproduced twice on a clean install."
)
BLOCK_HOOK = "tigon_url_block"


# ------------------------------------------------------------------- fixtures


def hook_entry(
    hook_id: str, deps: Iterable[str] = (), *, strategy: str = "url_block"
) -> dict[str, Any]:
    """One manifest hook `load_manifest` accepts, carrying its own probe call.

    `apply_unblock` writes through `write_manifest_atomically`, which loads the
    result before renaming it over the target, so a fixture the loader refuses
    would make every apply test pass for the wrong reason. Copied in shape from
    `tests/test_rulings.hook_entry`, which exists for the same reason.
    """

    marker = f"# {hook_id}"
    return {
        "hook_id": hook_id,
        "intent": "block a continuous-content surface",
        "tier": "robust",
        "strategy": strategy,
        "semantic_deps": list(deps),
        "hosts": [
            {"kind": "named", "descriptor": "Lcom/instagram/api/tigon/TigonServiceLayer;"}
        ],
        "anchor": ['const-string v0, "placeholder"'],
        "payload": [probe_call(hook_id), marker],
        "marker": marker,
        "expected_marker_count": 1,
    }


def _with_accent(document: dict[str, Any]) -> dict[str, Any]:
    """The same manifest with one non-ASCII character in it.

    `serialise` passes `ensure_ascii=False`, so a file written with the json
    module's default escaping differs from the canonical form only where a
    non-ASCII byte exists. Without one the two agree and the case tests nothing.
    """

    copied = json.loads(json.dumps(document))
    copied["hooks"][0]["intent"] += " — mesurée"
    return copied


def reversal_row(**overrides: Any) -> dict[str, Any]:
    """A complete, well-formed block withdrawal in on-disk shape."""

    row: dict[str, Any] = {
        "schema_version": 1,
        "withdraws": "block",
        "subject": SUBJECT_SLASHED,
        "original_decision_id": DOCKET,
        "decision_id": "withdraw-block-0123456789ab",
        "ruled_by": ARNAV,
        "rationale": RATIONALE,
        "recorded_at": WHEN,
    }
    row.update(overrides)
    return row


def a_reversal(**overrides: Any) -> Reversal:
    return Reversal.from_dict(reversal_row(**overrides))


def a_retirement_withdrawal(**overrides: Any) -> Reversal:
    fields: dict[str, Any] = {
        "withdraws": "retirement",
        "subject": DISCOVER,
        "effective_from": "446",
        "decision_id": "withdraw-retirement-0123456789ab",
    }
    fields.update(overrides)
    return a_reversal(**fields)


def retirement_row(
    hook_id: str,
    *,
    effective_from: str = "442",
    decision_id: str = DOCKET,
    ruled_by: str = ARNAV,
) -> dict[str, Any]:
    """One recorded retirement, in the flat shape `manifest/` holds."""

    return Retirement(
        hook_id=hook_id,
        effective_from=effective_from,
        decision_id=decision_id,
        ruled_by=ruled_by,
        rationale="The surface is gone from the app; the anchor is dead code.",
        recorded_at="2026-08-08T09:30:00Z",
    ).to_dict()


class ReversalTestCase(unittest.TestCase):
    """A temp tree, a load-able manifest, and a way to run `main` against it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        (self.tmp / "manifest").mkdir()
        self.reversals = self.tmp / REVERSALS
        self.retirements = self.tmp / RETIREMENTS

    # ------------------------------------------------------------- the corpus

    def manifest_at(
        self,
        deps: Iterable[str] = BLOCKED,
        *,
        name: str = "manifest/hooks.json",
        extra: Sequence[dict[str, Any]] = (),
        dumper: Any = serialise,
    ) -> Path:
        """`hooks.json` with one url-block hook carrying `deps`."""

        document = {
            "schema_version": 1,
            "hooks": [hook_entry(BLOCK_HOOK, deps), *extra],
        }
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumper(document), encoding="utf-8")
        return path

    def write_reversals(self, *rows: Mapping[str, Any] | str) -> Path:
        """Rows straight onto disk, so a reader can be tested without a writer."""

        self.reversals.parent.mkdir(parents=True, exist_ok=True)
        self.reversals.write_text(
            "".join(
                (row if isinstance(row, str) else json.dumps(row, sort_keys=True)) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        return self.reversals

    def write_retirements(self, *rows: Mapping[str, Any]) -> Path:
        self.retirements.parent.mkdir(parents=True, exist_ok=True)
        self.retirements.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return self.retirements

    # -------------------------------------------------------------- shortcuts

    def deps(self, path: Path, hook_id: str = BLOCK_HOOK) -> list[str]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return next(
            entry["semantic_deps"]
            for entry in document["hooks"]
            if entry["hook_id"] == hook_id
        )

    def rows(self, path: Path | None = None) -> list[dict[str, Any]]:
        location = self.reversals if path is None else path
        if not location.is_file():
            return []
        return [
            json.loads(line)
            for line in location.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = reversal_module.main(["--root", str(self.tmp), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def withdraw_block(self, manifest: Path, **overrides: str) -> tuple[int, str, str]:
        arguments = {
            "--endpoint": SUBJECT,
            "--manifest": str(manifest),
            "--original-decision-id": DOCKET,
            "--ruled-by": ARNAV,
            "--rationale": RATIONALE,
            "--recorded-at": WHEN,
        }
        arguments.update(overrides)
        flat = [part for pair in arguments.items() for part in pair]
        return self.run_main("withdraw-block", *flat, "--confirm")

    def withdraw_retirement(self, **overrides: str) -> tuple[int, str, str]:
        arguments = {
            "--hook": DISCOVER,
            "--from-version": "446",
            "--original-decision-id": DOCKET,
            "--ruled-by": ARNAV,
            "--rationale": RATIONALE,
            "--recorded-at": WHEN,
        }
        arguments.update(overrides)
        flat = [part for pair in arguments.items() for part in pair]
        return self.run_main("withdraw-retirement", *flat, "--confirm")


class CorpusTestCase(RetirementTestCase):
    """`tests/test_retirement`'s evidence tree, plus the two ledgers this joins.

    Inherited rather than rebuilt: `candidates`, `build_case` and `compare` all
    read that tree, and a second fixture for the same layout is a second place
    for the layout to be wrong.
    """

    def write_retirements(self, *rows: Mapping[str, Any]) -> Path:
        path = self.tmp / RETIREMENTS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def write_reversals(self, *rows: Reversal) -> Path:
        path = self.tmp / REVERSALS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def four_ports(self) -> None:
        """439, 444, 445, 446 — the span a withdrawal ruled for 446 straddles.

        The versions between 439 and 444 are deliberately absent: `_predecessor`
        is whatever the tree holds, so a shorter series measures the same
        boundary with four ports instead of eight. `TIGON` is release-ready
        through 445 and fails its probe on 446, which is the only shape in which
        `compare` can say `retired` and `dropped` about the same hook.
        """

        self.baseline_port()
        self.port("444", {CONTEXT: triple(), TIGON: triple()}, previous="439")
        self.port("445", {CONTEXT: triple(), TIGON: triple()}, previous="444")
        self.port(
            "446",
            {CONTEXT: triple(), TIGON: triple(runtime_probe="failed")},
            previous="445",
        )

    def states(self, comparison: Any) -> dict[str, str]:
        return {item.hook_id: item.state for item in comparison.verdicts}


# ================================================================== the join key


class JoinKeyTests(ReversalTestCase):
    """One decision id, many subjects. Withdrawing one withdraws exactly one.

    The single most important property in this module and the one with the least
    visible failure: keyed on the decision id alone, withdrawing one ruling out
    of a docket silently withdraws every other ruling in it, and each of those
    looks in every other respect like a decision a human made.

    Both kinds are attacked, and both through a consumer. `withdrawn` returning
    the right dictionary is necessary and not sufficient — `retired_by` does the
    lookup, and a pair-keyed producer read by an id-keyed consumer is the same
    bug one layer along.
    """

    def test_withdrawing_one_endpoint_leaves_the_five_siblings_blocked(self):
        """Six rulings, one decision id, one withdrawal. Five stay in the manifest."""
        manifest = self.manifest_at()

        code, out, err = self.withdraw_block(manifest)

        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(
            self.deps(manifest),
            [
                "/feed/timeline/",
                "/discover/topical_explore",
                "/api/v1/clips/homecoming/",
                "feed/reels_media_stream/",
                "delivery/background_prefetch",
            ],
        )
        self.assertNotIn(SUBJECT, self.deps(manifest))
        self.assertEqual(len(self.deps(manifest)), len(BLOCKED) - 1)

    def test_the_store_is_keyed_on_the_pair_and_holds_one_key_per_subject(self):
        """Two withdrawals under one decision id are two entries, not one."""
        self.write_reversals(
            reversal_row(subject=SUBJECT, decision_id="withdraw-block-aaaaaaaaaaaa"),
            reversal_row(
                subject="/feed/timeline/", decision_id="withdraw-block-bbbbbbbbbbbb"
            ),
        )

        found = withdrawn("block", self.tmp)

        self.assertEqual(
            sorted(found),
            [(DOCKET, "/feed/timeline/"), (DOCKET, SUBJECT)],
        )
        self.assertEqual(len(found), 2)

    def test_a_withdrawal_of_one_hook_does_not_unretire_its_docket_siblings(self):
        """Two hooks retired under one decision. One withdrawal restores one hook.

        Asserted through `retirements_in_force`, which is what every consumer
        calls. A `withdrawn` map keyed correctly and read by an id-keyed
        `retired_by` would pass a test that stopped at `reversal`.
        """
        self.write_retirements(
            retirement_row(DISCOVER, effective_from="442"),
            retirement_row(TIGON, effective_from="442"),
            retirement_row(CONTEXT, effective_from="442"),
        )
        before = sorted(retirements_in_force("446", self.tmp))

        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="446").to_dict()
        )
        after = sorted(retirements_in_force("446", self.tmp))

        self.assertEqual(before, sorted([CONTEXT, DISCOVER, TIGON]))
        self.assertEqual(after, sorted([CONTEXT, TIGON]))

    def test_the_same_subject_under_a_different_decision_is_a_different_key(self):
        """`(id, subject)` is a pair in both directions, not only the first one."""
        self.write_retirements(
            retirement_row(DISCOVER, effective_from="442", decision_id=DOCKET)
        )
        self.write_reversals(
            a_retirement_withdrawal(
                subject=DISCOVER,
                original_decision_id=OTHER_DOCKET,
                effective_from="442",
            ).to_dict()
        )

        found = retirements_in_force("446", self.tmp)

        self.assertIn(DISCOVER, found)
        self.assertEqual(found[DISCOVER].decision_id, DOCKET)

    def test_withdrawing_every_subject_of_a_docket_takes_them_all_and_needs_a_row_each(self):
        """The control for the test above: six rows do withdraw six rulings.

        Without it, a `withdrawn` that returned nothing at all would pass every
        sibling assertion in this class — an absence assertion with no positive
        control always passes.
        """
        manifest = self.manifest_at()

        for index, endpoint in enumerate(BLOCKED):
            code, out, err = self.withdraw_block(
                manifest, **{"--endpoint": endpoint}
            )
            self.assertEqual((code, err), (0, ""), f"{endpoint}: {out}")
            self.assertEqual(len(self.deps(manifest)), len(BLOCKED) - index - 1)

        self.assertEqual(self.deps(manifest), [])
        self.assertEqual(len(self.rows()), len(BLOCKED))
        self.assertEqual({row["original_decision_id"] for row in self.rows()}, {DOCKET})
        self.assertEqual(len(withdrawn("block", self.tmp)), len(BLOCKED))

    def test_two_hooks_retired_by_one_decision_need_two_rows_to_both_come_back(self):
        """The retirement-side control. One row restores one hook, two restore two."""
        self.write_retirements(
            retirement_row(DISCOVER, effective_from="442"),
            retirement_row(TIGON, effective_from="442"),
        )

        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="442").to_dict()
        )
        one = sorted(retirements_in_force("442", self.tmp))
        self.write_reversals(
            a_retirement_withdrawal(
                subject=DISCOVER,
                effective_from="442",
                decision_id="withdraw-retirement-aaaaaaaaaaaa",
            ).to_dict(),
            a_retirement_withdrawal(
                subject=TIGON,
                effective_from="442",
                decision_id="withdraw-retirement-bbbbbbbbbbbb",
            ).to_dict(),
        )
        both = sorted(retirements_in_force("442", self.tmp))

        self.assertEqual(one, [TIGON])
        self.assertEqual(both, [])


# ============================================================== effective_from


class EffectiveFromTests(ReversalTestCase):
    """One field, required on one kind and refused on the other.

    The asymmetry is argued in the module docstring rather than inherited:
    `expectation` asks whether a hook was retired *as of a version*, so restoring
    it has to name one; `manifest/hooks.json` is applied to whatever version is
    being built and a version field there would be a number nothing reads. Both
    directions are pinned, because a rule enforced in one direction only reads as
    "the field is optional".
    """

    def test_a_retirement_withdrawal_without_a_version_is_refused(self):
        with self.assertRaises(ReversalError) as caught:
            a_reversal(withdraws="retirement", subject=DISCOVER)

        self.assertIn("effective_from", str(caught.exception))
        self.assertIn("retired as of a version", str(caught.exception))

    def test_a_retirement_withdrawal_with_a_non_numeric_version_is_refused(self):
        for value in ("44a", "v446", " 446", "446.0", ""):
            with self.subTest(value=value):
                with self.assertRaises(ReversalError):
                    a_reversal(
                        withdraws="retirement", subject=DISCOVER, effective_from=value
                    )

    def test_a_block_withdrawal_carrying_a_version_is_refused(self):
        with self.assertRaises(ReversalError) as caught:
            a_reversal(effective_from="446")

        self.assertIn("must not carry effective_from", str(caught.exception))
        self.assertIn("nothing reads", str(caught.exception))

    def test_a_block_withdrawal_omits_the_field_from_its_row_rather_than_writing_it_empty(self):
        """Omitted, not `""`. An empty string invites a reader to treat it as a version."""
        row = a_reversal().to_dict()

        self.assertNotIn("effective_from", row)
        self.assertEqual(a_reversal().effective_from, "")

    def test_a_retirement_withdrawal_writes_the_field_it_requires(self):
        row = a_retirement_withdrawal(effective_from="446").to_dict()

        self.assertEqual(row["effective_from"], "446")

    def test_the_block_subcommand_has_no_version_flag_to_pass(self):
        """The asymmetry reaches the interface, not only the dataclass."""
        manifest = self.manifest_at()

        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.withdraw_block(manifest, **{"--from-version": "446"})

        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.rows(), [])

    def test_the_retirement_subcommand_cannot_be_run_without_one(self):
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.run_main(
                    "withdraw-retirement",
                    "--hook", DISCOVER,
                    "--original-decision-id", DOCKET,
                    "--ruled-by", ARNAV,
                    "--rationale", RATIONALE,
                    "--recorded-at", WHEN,
                    "--confirm",
                )

        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.rows(), [])


# ================================================================ in force when


class InForceTests(ReversalTestCase):
    """A withdrawal ruled for 446 changes 446 and nothing before it.

    `withdrawn_at` is the version filter and `retirements_in_force` is the join.
    Both are swept across the boundary rather than sampled at it: a filter that
    was inclusive on the wrong side, or ignored the version entirely, agrees with
    a single-version assertion.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_retirements(
            retirement_row(DISCOVER, effective_from="442"),
            retirement_row(TIGON, effective_from="442"),
        )

    def test_before_the_row_exists_both_retirements_hold_across_the_span(self):
        """The control. Every version below reads the same without a withdrawal."""
        for version in ("442", "443", "444", "445", "446", "447"):
            with self.subTest(version=version):
                self.assertEqual(
                    sorted(retirements_in_force(version, self.tmp)),
                    sorted([DISCOVER, TIGON]),
                )

    def test_a_withdrawal_ruled_for_446_leaves_442_through_445_untouched(self):
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="446").to_dict()
        )

        for version in ("442", "443", "444", "445"):
            with self.subTest(version=version):
                self.assertEqual(
                    sorted(retirements_in_force(version, self.tmp)),
                    sorted([DISCOVER, TIGON]),
                )
                self.assertEqual(withdrawn_at(version, "retirement", self.tmp), {})

    def test_the_same_withdrawal_restores_the_hook_at_446_and_after(self):
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="446").to_dict()
        )

        for version in ("446", "447", "500"):
            with self.subTest(version=version):
                self.assertEqual(
                    sorted(retirements_in_force(version, self.tmp)), [TIGON]
                )
                self.assertEqual(
                    sorted(withdrawn_at(version, "retirement", self.tmp)),
                    [(DOCKET, DISCOVER)],
                )

    def test_the_boundary_version_is_included_and_not_the_one_below_it(self):
        """`<=`, stated once and directly, because an off-by-one here is silent."""
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="446").to_dict()
        )

        self.assertIn(DISCOVER, retirements_in_force("445", self.tmp))
        self.assertNotIn(DISCOVER, retirements_in_force("446", self.tmp))

    def test_versions_are_compared_as_numbers_and_not_as_strings(self):
        """`"1000" < "446"` as text. `history.series` documents the same trap."""
        self.write_retirements(retirement_row(DISCOVER, effective_from="442"))
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="1000").to_dict()
        )

        self.assertIn(DISCOVER, retirements_in_force("446", self.tmp))
        self.assertNotIn(DISCOVER, retirements_in_force("1000", self.tmp))

    def test_withdrawn_at_refuses_a_version_that_is_not_a_number(self):
        for value in ("44a", "", "v446", "446.0", "-446"):
            with self.subTest(value=value):
                with self.assertRaises(ReversalError) as caught:
                    withdrawn_at(value, "retirement", self.tmp)
                self.assertIn("is not a version number", str(caught.exception))

    def test_withdrawn_at_never_returns_a_block_because_a_block_has_no_version(self):
        """`withdrawn` sees it and `withdrawn_at` cannot, which is the design."""
        self.write_reversals(reversal_row())

        self.assertEqual(list(withdrawn("block", self.tmp)), [(DOCKET, SUBJECT_SLASHED)])
        self.assertEqual(withdrawn_at("446", "block", self.tmp), {})
        self.assertEqual(withdrawn_at("999999", "block", self.tmp), {})

    def test_a_block_withdrawal_cannot_unretire_a_hook_that_shares_its_name(self):
        """The kind is part of the join, not decoration.

        Nothing forbids an endpoint and a hook carrying the same string, and the
        two ledgers are joined on `(decision_id, name)` with no type in it. Only
        the kind filter keeps a withdrawn block from answering a question about a
        retirement — and both rows here name the same docket, which is exactly
        the collision a shared decision id makes likely rather than exotic.
        """
        self.write_reversals(reversal_row(subject=DISCOVER))

        self.assertEqual(list(withdrawn("block", self.tmp)), [(DOCKET, DISCOVER)])
        self.assertEqual(withdrawn("retirement", self.tmp), {})
        self.assertIn(DISCOVER, retirements_in_force("446", self.tmp))

    def test_withdrawn_filters_by_kind_and_refuses_a_kind_it_does_not_know(self):
        self.write_reversals(
            reversal_row(),
            a_retirement_withdrawal(effective_from="446").to_dict(),
        )

        self.assertEqual(list(withdrawn("block", self.tmp)), [(DOCKET, SUBJECT_SLASHED)])
        self.assertEqual(list(withdrawn("retirement", self.tmp)), [(DOCKET, DISCOVER)])
        self.assertEqual(KINDS, ("block", "retirement"))
        with self.assertRaises(ReversalError) as caught:
            withdrawn("offer_toggle", self.tmp)
        self.assertIn("unknown reversal kind", str(caught.exception))

    def test_retired_by_ignores_a_withdrawal_of_a_decision_that_is_not_the_retirements(self):
        """The parameter is a join, not a set of hook ids."""
        retirements = read_retirements(self.tmp)

        matched = retired_by(
            "446", retirements, withdrawn={(DOCKET, DISCOVER): object()}
        )
        mismatched = retired_by(
            "446", retirements, withdrawn={(OTHER_DOCKET, DISCOVER): object()}
        )

        self.assertEqual(sorted(matched), [TIGON])
        self.assertEqual(sorted(mismatched), sorted([DISCOVER, TIGON]))

    def test_retirements_in_force_reads_both_files_from_one_root(self):
        """One function because two files decide this. Neither file alone answers."""
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="442").to_dict()
        )

        self.assertIn(DISCOVER, read_retirements(self.tmp))
        self.assertNotIn(DISCOVER, retirements_in_force("442", self.tmp))


# =================================================================== consumers


class ConsumerTests(CorpusTestCase):
    """Every reader of "is this hook retired" honours a withdrawal.

    A reversal store nothing reads records an intention and changes nothing,
    which is the shape this project has shipped at one end or the other four
    times. Each consumer is asserted against a before-and-after control in the
    same corpus, because a corpus in which the hook was never retired and one in
    which the withdrawal worked look identical from the outside.
    """

    def four_forty_two(self) -> None:
        """441's corpus plus a 442 in which neither candidate hook is ready."""
        self.ordinary_corpus()
        self.port(
            "442",
            {
                CONTEXT: triple(),
                TIGON: triple(runtime_probe="failed"),
                DISCOVER: triple(runtime_probe="inconclusive"),
            },
            previous="441",
        )

    def test_a_hook_whose_retirement_was_withdrawn_is_a_candidate_again(self):
        """Before and after the reversal row, in one corpus."""
        self.four_forty_two()
        self.write_retirements(retirement_row(DISCOVER, effective_from="442"))
        before = [item.hook_id for item in candidates(self.tmp, version="442")]

        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="442")
        )
        after = [item.hook_id for item in candidates(self.tmp, version="442")]

        self.assertNotIn(DISCOVER, before)
        self.assertIn(DISCOVER, after)

    def test_the_docket_sibling_stays_off_the_candidate_list(self):
        """The same six-rulings-one-id property, seen from `retirement`."""
        self.four_forty_two()
        self.write_retirements(
            retirement_row(DISCOVER, effective_from="442"),
            retirement_row(TIGON, effective_from="442"),
        )
        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="442")
        )

        found = [item.hook_id for item in candidates(self.tmp, version="442")]

        self.assertIn(DISCOVER, found)
        self.assertNotIn(TIGON, found)

    def test_a_case_can_be_built_for_a_hook_whose_retirement_was_withdrawn(self):
        """And cannot be, while the retirement is in force. Both in one corpus."""
        self.four_forty_two()
        self.write_retirements(retirement_row(DISCOVER, effective_from="442"))

        with self.assertRaises(RetirementError) as caught:
            build_case(
                self.tmp,
                hook_id=DISCOVER,
                version="442",
                investigation=self.build(DISCOVER, "441").investigation,
            )
        self.assertIn("already retired at 442", str(caught.exception))

        self.write_reversals(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="442")
        )
        case = build_case(
            self.tmp,
            hook_id=DISCOVER,
            version="442",
            investigation=self.build(DISCOVER, "441").investigation,
        )

        self.assertEqual(case.hook_id, DISCOVER)
        self.assertEqual(case.effective_from, "443")

    def test_compare_calls_a_retired_hook_retired_and_a_withdrawn_one_dropped(self):
        """The whole point, at the module that fails the port.

        Three readings of one corpus: no reversal, a reversal effective at 446,
        and a reversal effective at 447. Only the middle one restores the hook,
        and the third is the proof that nothing is restored retroactively.
        """
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))

        plain = compare_at(self.tmp, "446", "445")
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="446")
        )
        withdrawn_now = compare_at(self.tmp, "446", "445")
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="447")
        )
        withdrawn_later = compare_at(self.tmp, "446", "445")

        self.assertEqual(self.states(plain)[TIGON], "retired")
        self.assertTrue(plain.met)
        self.assertEqual(self.states(withdrawn_now)[TIGON], "dropped")
        self.assertFalse(withdrawn_now.met)
        self.assertEqual(withdrawn_now.dropped, (TIGON,))
        self.assertEqual(self.states(withdrawn_later)[TIGON], "retired")
        self.assertTrue(withdrawn_later.met)

    def test_compare_leaves_the_ports_below_the_withdrawal_exactly_as_they_were(self):
        """445 reads the same with and without a row that takes effect at 446."""
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))

        before = compare_at(self.tmp, "445", "444").to_dict()
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="446")
        )
        after = compare_at(self.tmp, "445", "444").to_dict()

        self.assertEqual(before, after)
        self.assertEqual(self.states(compare_at(self.tmp, "445", "444"))[TIGON],
                         "retired_still_passing")

    def test_the_expectation_cli_fails_the_port_the_withdrawal_restored(self):
        """Exit 3, not 0 — the drop and the withdrawal reach the interface."""
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))

        met = run_expectation(self.tmp, "--version", "446")
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="446")
        )
        dropped = run_expectation(self.tmp, "--version", "446")

        self.assertEqual(met[0], 0, met)
        self.assertEqual(dropped[0], 3, dropped)
        self.assertIn(TIGON, dropped[1])

    def test_an_explicit_retirements_argument_still_bypasses_the_reversal_store(self):
        """Documented, and pinned so the bypass cannot widen unnoticed.

        `compare(retirements=...)` takes the caller's dictionary and calls
        `retired_by` without a `withdrawn` map. That is what makes
        :class:`KnownDefectTests` 1 possible, and it is asserted here so the
        defect has a positive control: if the argument ever starts honouring the
        store, this test fails and that one succeeds.
        """
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="446")
        )

        handed = expectation_module.compare(
            self.tmp,
            version="446",
            previous="445",
            retirements=read_retirements(self.tmp),
        )

        self.assertEqual(self.states(handed)[TIGON], "retired")
        self.assertTrue(handed.met)


def compare_at(root: Path, version: str, previous: str) -> Any:
    return expectation_module.compare(root, version=version, previous=previous)


def run_expectation(root: Path, *args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = expectation_module.main(["--root", str(root), *args])
    return code, stdout.getvalue(), stderr.getvalue()


# ===================================================================== signing


class SigningTests(ReversalTestCase):
    """A human withdraws a decision. The refusals are the mechanism.

    Same rule and same reason as `retirement.validate_ruling`: if the thing being
    measured could withdraw the measurement, the cheapest route past a red build
    would be to undo the rule rather than fix the hook. The casing sweep is not
    padding — `ruled_by` arrives from a command line, and `Agent` is what a human
    typing a name would produce.
    """

    def test_ruled_by_agent_is_refused_in_every_casing(self):
        for value in ("agent", "Agent", "AGENT", "aGeNt", " agent ", "\tAgent\n"):
            with self.subTest(value=value):
                with self.assertRaises(ReversalError) as caught:
                    a_reversal(ruled_by=value)
                self.assertIn("ruled_by is 'agent'", str(caught.exception))
                self.assertIn("A human withdraws a decision", str(caught.exception))

    def test_a_name_that_merely_contains_agent_is_a_person_and_is_accepted(self):
        """The check is the whole value, not a substring. `agentina` is a name."""
        for value in ("agent:claude-opus-5", "agentina", "arnav (agent-assisted)"):
            with self.subTest(value=value):
                self.assertEqual(a_reversal(ruled_by=value).ruled_by, value)

    def test_the_cli_refuses_an_agent_and_writes_nothing(self):
        manifest = self.manifest_at()

        code, _, err = self.withdraw_block(manifest, **{"--ruled-by": "AGENT"})

        self.assertEqual(code, 2)
        self.assertIn("ruled_by is 'agent'", err)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.deps(manifest), list(BLOCKED))

    def test_each_required_field_is_refused_when_absent(self):
        for field in (
            "subject",
            "original_decision_id",
            "decision_id",
            "ruled_by",
            "rationale",
        ):
            with self.subTest(field=field):
                row = reversal_row()
                del row[field]
                with self.assertRaises(ReversalError) as caught:
                    Reversal.from_dict(row)
                self.assertIn(f"missing {field}", str(caught.exception))

    def test_each_required_field_is_refused_when_blank_or_whitespace(self):
        for field in (
            "subject",
            "original_decision_id",
            "decision_id",
            "ruled_by",
            "rationale",
        ):
            for value in ("", "   ", "\t", "\n", " \r\n "):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ReversalError) as caught:
                        a_reversal(**{field: value})
                    self.assertIn(f"missing {field}", str(caught.exception))

    def test_the_refusal_says_why_a_field_matters_rather_than_naming_a_schema(self):
        with self.assertRaises(ReversalError) as caught:
            a_reversal(rationale="")

        self.assertIn("an edit wearing a record's clothes", str(caught.exception))

    def test_recorded_at_is_the_one_field_that_may_be_empty(self):
        """Deliberate: `REVERSALS.md` says "everything except `recorded_at`".

        Pinned so the asymmetry is a decision on the record rather than an
        omission — see :class:`KnownDefectTests` in `tests/test_retirement.py`,
        where the same field being unchecked in one of two authorities was a
        defect.
        """
        item = a_reversal(recorded_at="")

        self.assertEqual(item.recorded_at, "")
        self.assertEqual(item.to_dict()["recorded_at"], "")

    def test_an_unknown_kind_is_refused_and_the_message_lists_the_known_ones(self):
        for value in ("unblock", "", "Block", "offer_toggle", "ruling"):
            with self.subTest(value=value):
                with self.assertRaises(ReversalError) as caught:
                    a_reversal(withdraws=value)
                self.assertIn("unknown reversal kind", str(caught.exception))
                self.assertIn("block, retirement", str(caught.exception))


# ================================================================== the record


class RecordTests(ReversalTestCase):
    """Parsing, identity and appending. The store is append-only and refuses twice.

    `read_reversals` refuses a row it cannot read rather than skipping it, and
    the direction matters: a skipped withdrawal reads as "this decision was never
    withdrawn", which is the direction that keeps a block in force. A skip for
    "absent" that swallowed "unreadable" is a failure this project has shipped.
    """

    def test_a_complete_row_round_trips_through_to_dict_and_from_dict(self):
        item = a_reversal()

        self.assertEqual(Reversal.from_dict(item.to_dict()), item)
        self.assertEqual(item.to_dict(), reversal_row())

    def test_a_retirement_row_round_trips_with_its_version(self):
        item = a_retirement_withdrawal(effective_from="446")

        self.assertEqual(Reversal.from_dict(item.to_dict()), item)

    def test_from_dict_refuses_an_unknown_key_and_names_it(self):
        with self.assertRaises(ReversalError) as caught:
            Reversal.from_dict(reversal_row(hook_id=DISCOVER, note="typo"))

        self.assertIn("unknown keys", str(caught.exception))
        self.assertIn("hook_id", str(caught.exception))
        self.assertIn("note", str(caught.exception))

    def test_from_dict_refuses_anything_that_is_not_an_object(self):
        for value in ([], "a string", 3, None, True):
            with self.subTest(value=value):
                with self.assertRaises(ReversalError) as caught:
                    Reversal.from_dict(value)
                self.assertIn("must be a JSON object", str(caught.exception))

    def test_from_dict_refuses_a_schema_version_that_is_not_the_integer_one(self):
        for value in (None, 0, 2, "1", 1.0, True):
            with self.subTest(value=value):
                row = reversal_row()
                row["schema_version"] = value
                if value in (1.0, True):
                    # `1.0 == 1` and `True == 1` in Python; the check is `!= 1`,
                    # so these pass. Pinned rather than asserted as a refusal,
                    # because nothing writes them and inventing a refusal here
                    # would be testing a rule the module does not have.
                    self.assertEqual(Reversal.from_dict(row).schema_version, value)
                    continue
                with self.assertRaises(ReversalError) as caught:
                    Reversal.from_dict(row)
                self.assertIn("unsupported reversal schema", str(caught.exception))

    def test_read_reversals_on_a_missing_file_is_empty_and_not_an_error(self):
        self.assertEqual(read_reversals(self.tmp), [])
        self.assertEqual(read_reversals(self.tmp, path=self.tmp / "nowhere.jsonl"), [])
        self.assertFalse(self.reversals.exists())

    def test_a_malformed_line_is_refused_and_the_message_names_the_file_and_line(self):
        self.write_reversals(reversal_row(), "{not json", reversal_row())

        with self.assertRaises(ReversalError) as caught:
            read_reversals(self.tmp)

        self.assertIn(str(self.reversals), str(caught.exception))
        self.assertIn(":2:", str(caught.exception))

    def test_a_row_that_is_not_an_object_is_refused_with_its_line(self):
        self.write_reversals(reversal_row(), "[1, 2]")

        with self.assertRaises(ReversalError) as caught:
            read_reversals(self.tmp)

        self.assertIn(":2: expected a JSON object, got list", str(caught.exception))

    def test_a_row_whose_fields_are_wrong_is_refused_with_its_line(self):
        self.write_reversals(reversal_row(), reversal_row(ruled_by="agent"))

        with self.assertRaises(ReversalError) as caught:
            read_reversals(self.tmp)

        self.assertIn(":2:", str(caught.exception))
        self.assertIn("ruled_by is 'agent'", str(caught.exception))

    def test_blank_lines_are_skipped_and_do_not_shift_the_line_numbers(self):
        self.reversals.write_text(
            "\n"
            + json.dumps(reversal_row(), sort_keys=True)
            + "\n   \n"
            + "{not json\n",
            encoding="utf-8",
        )

        with self.assertRaises(ReversalError) as caught:
            read_reversals(self.tmp)

        self.assertIn(":4:", str(caught.exception))

    def test_a_row_reads_the_same_wrapped_in_a_record_or_written_flat(self):
        """Both shapes exist on disk in this project; `read_retirements` accepts both."""
        self.write_reversals(
            reversal_row(),
            {"record": reversal_row(subject="/feed/timeline/")},
        )

        found = read_reversals(self.tmp)

        self.assertEqual([item.subject for item in found],
                         [SUBJECT_SLASHED, "/feed/timeline/"])

    def test_a_record_wrapper_holding_something_that_is_not_an_object_is_refused(self):
        self.write_reversals({"record": [1, 2]})

        with self.assertRaises(ReversalError) as caught:
            read_reversals(self.tmp)

        self.assertIn(":1:", str(caught.exception))
        self.assertIn("must be a JSON object", str(caught.exception))

    def test_rows_come_back_in_file_order(self):
        self.write_reversals(
            reversal_row(subject="/feed/timeline/"),
            reversal_row(subject=SUBJECT),
            reversal_row(subject="delivery/background_prefetch"),
        )

        self.assertEqual(
            [item.subject for item in read_reversals(self.tmp)],
            ["/feed/timeline/", SUBJECT, "delivery/background_prefetch"],
        )

    # ------------------------------------------------------------- identity

    def test_identical_answers_collide_and_a_retry_cannot_mint_a_second_id(self):
        arguments = dict(
            withdraws="block",
            subject=SUBJECT,
            original_decision_id=DOCKET,
            ruled_by=ARNAV,
            rationale=RATIONALE,
            recorded_at=WHEN,
        )

        self.assertEqual(reversal_id(**arguments), reversal_id(**arguments))
        self.assertTrue(reversal_id(**arguments).startswith("withdraw-block-"))

    def test_every_field_of_the_answer_moves_the_id(self):
        arguments: dict[str, str] = dict(
            withdraws="retirement",
            subject=DISCOVER,
            original_decision_id=DOCKET,
            ruled_by=ARNAV,
            rationale=RATIONALE,
            recorded_at=WHEN,
            effective_from="446",
        )
        base = reversal_id(**arguments)

        for field, value in (
            ("withdraws", "block"),
            ("subject", TIGON),
            ("original_decision_id", OTHER_DOCKET),
            ("ruled_by", "someone-else"),
            ("rationale", RATIONALE + "."),
            ("recorded_at", "2026-08-10T10:00:00Z"),
            ("effective_from", "447"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(reversal_id(**{**arguments, field: value}), base)

    def test_the_subject_is_in_the_digest_so_two_rulings_of_one_docket_differ(self):
        """The identity has the same pair property the join key does."""
        common = dict(
            withdraws="block",
            original_decision_id=DOCKET,
            ruled_by=ARNAV,
            rationale=RATIONALE,
            recorded_at=WHEN,
        )

        self.assertNotEqual(
            reversal_id(subject=SUBJECT, **common),
            reversal_id(subject="/feed/timeline/", **common),
        )

    # --------------------------------------------------------------- append

    def test_append_creates_the_directory_and_writes_one_line(self):
        location = self.tmp / "elsewhere" / "reversals.jsonl"

        written = append(a_reversal(), path=location)

        self.assertEqual(written, location)
        self.assertEqual(len(self.rows(location)), 1)
        self.assertEqual(self.rows(location)[0], reversal_row())

    def test_append_adds_rather_than_truncates(self):
        append(a_reversal(subject=SUBJECT), root=self.tmp)
        append(a_reversal(subject="/feed/timeline/"), root=self.tmp)

        self.assertEqual(len(self.rows()), 2)

    def test_append_refuses_a_second_withdrawal_of_the_same_pair(self):
        append(a_reversal(), root=self.tmp)

        with self.assertRaises(ReversalError) as caught:
            append(
                a_reversal(decision_id="withdraw-block-ffffffffffff", ruled_by="someone"),
                root=self.tmp,
            )

        self.assertIn("was already withdrawn", str(caught.exception))
        self.assertIn(ARNAV, str(caught.exception))
        self.assertIn("rule on it at the gate", str(caught.exception))
        self.assertEqual(len(self.rows()), 1)

    def test_append_allows_the_same_subject_under_a_different_decision(self):
        append(a_reversal(), root=self.tmp)

        append(a_reversal(original_decision_id=OTHER_DOCKET), root=self.tmp)

        self.assertEqual(len(self.rows()), 2)

    def test_append_allows_a_different_subject_under_the_same_decision(self):
        append(a_reversal(subject=SUBJECT), root=self.tmp)

        append(a_reversal(subject="/feed/timeline/"), root=self.tmp)

        self.assertEqual(len(self.rows()), 2)

    def test_append_separates_the_two_kinds_of_withdrawal_of_one_subject(self):
        """A hook and an endpoint could share a name; the kinds do not collide."""
        append(a_reversal(subject=DISCOVER), root=self.tmp)

        append(
            a_retirement_withdrawal(subject=DISCOVER, effective_from="446"),
            root=self.tmp,
        )

        self.assertEqual(len(self.rows()), 2)

    def test_append_honours_path_over_root(self):
        location = self.tmp / "chosen.jsonl"

        append(a_reversal(), root=self.tmp, path=location)

        self.assertTrue(location.is_file())
        self.assertFalse(self.reversals.exists())


# ================================================================= the unblock


class UnblockPlanTests(ReversalTestCase):
    """Finding the manifest entry the way the app finds it, or refusing.

    `assessment.normalise` strips a leading slash and an `api/v1/` prefix, and a
    raw-string comparison here fails in the worst available direction: the
    reversal records cleanly, the human is told nothing went wrong, and the
    endpoint stays blocked. `a-leading-slash-hid-a-whole-grouping` is the same
    mistake costing four candidates on 440.
    """

    def test_a_bare_subject_withdraws_a_slashed_manifest_entry(self):
        manifest = self.manifest_at(["/feed/timeline_stream/", "/feed/timeline/"])

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        self.assertEqual(plan.removed, ("/feed/timeline_stream/",))
        self.assertEqual(plan.hook_ids, (BLOCK_HOOK,))
        self.assertEqual(
            json.loads(plan.document_after)["hooks"][0]["semantic_deps"],
            ["/feed/timeline/"],
        )

    def test_a_slashed_subject_withdraws_a_bare_manifest_entry(self):
        manifest = self.manifest_at(["feed/timeline_stream/", "/feed/timeline/"])

        plan = plan_unblock(a_reversal(subject=SUBJECT_SLASHED), manifest_path=manifest)

        self.assertEqual(plan.removed, ("feed/timeline_stream/",))

    def test_the_api_v1_prefix_is_stripped_on_both_sides_as_the_app_strips_it(self):
        manifest = self.manifest_at(BLOCKED)

        plan = plan_unblock(
            a_reversal(subject=PREFIXED_SUBJECT), manifest_path=manifest
        )

        self.assertEqual(plan.removed, (PREFIXED_IN_MANIFEST,))
        self.assertEqual(normalise(PREFIXED_IN_MANIFEST), normalise(PREFIXED_SUBJECT))

    def test_the_recorded_subject_is_the_decisions_spelling_not_the_manifests(self):
        """Both spellings survive: the row says what was decided, `removed` what changed."""
        manifest = self.manifest_at(BLOCKED)

        plan = plan_unblock(a_reversal(subject=SUBJECT_SLASHED), manifest_path=manifest)

        self.assertEqual(plan.reversal.subject, SUBJECT_SLASHED)
        self.assertEqual(plan.removed, (SUBJECT,))

    def test_an_endpoint_in_no_hooks_semantic_deps_is_refused(self):
        manifest = self.manifest_at(BLOCKED)

        for subject in ("feed/nowhere/", "timeline_stream", "feed/timeline_stream"):
            with self.subTest(subject=subject):
                with self.assertRaises(ReversalError) as caught:
                    plan_unblock(a_reversal(subject=subject), manifest_path=manifest)
                self.assertIn("is not in any hook's semantic_deps", str(caught.exception))
                self.assertIn("would record an intention", str(caught.exception))

    def test_a_near_miss_is_a_refusal_and_not_a_neighbouring_removal(self):
        """`feed/timeline/` and `feed/timeline_stream/` are different rules.

        The trailing slash is part of the rule — that is exactly why
        `feed/timeline_stream/` was a gap on 439 while `/feed/timeline/` was
        already blocked — so a subject must not withdraw its prefix.
        """
        manifest = self.manifest_at(["/feed/timeline/"])

        with self.assertRaises(ReversalError):
            plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        self.assertEqual(self.deps(manifest), ["/feed/timeline/"])

    def test_a_manifest_that_is_not_in_canonical_form_is_refused(self):
        forms = {
            "four-space indent": lambda d: json.dumps(d, indent=4) + "\n",
            "no trailing newline": lambda d: json.dumps(d, indent=2),
            "one long line": lambda d: json.dumps(d) + "\n",
            "escaped non-ascii": lambda d: json.dumps(
                _with_accent(d), indent=2, ensure_ascii=True
            ) + "\n",
        }
        for label, dumper in forms.items():
            with self.subTest(form=label):
                manifest = self.manifest_at(BLOCKED, dumper=dumper)
                with self.assertRaises(ReversalError) as caught:
                    plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)
                self.assertIn("not in canonical form", str(caught.exception))
                self.assertIn("reformat lines nobody reviewed", str(caught.exception))

    def test_the_guard_is_a_byte_round_trip_and_not_a_key_order_policy(self):
        """A sorted-key manifest is accepted, and that is right rather than a hole.

        `json.loads` preserves the file's key order into the dict, so
        re-serialising a sorted file reproduces it byte for byte. The question
        the guard asks is "would writing this reformat lines nobody reviewed",
        and the answer here is no. Pinned because the obvious reading of the
        refusal — "the manifest must be in one blessed key order" — is wrong, and
        a future tightening would break every real manifest that is not sorted.
        """
        manifest = self.manifest_at(
            BLOCKED, dumper=lambda d: json.dumps(d, sort_keys=True, indent=2) + "\n"
        )

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        self.assertEqual(plan.removed, (SUBJECT,))
        self.assertEqual(plan.document_after, serialise(json.loads(plan.document_after)))

    def test_a_retirement_withdrawal_is_refused_by_the_block_planner(self):
        manifest = self.manifest_at(BLOCKED)

        with self.assertRaises(ReversalError) as caught:
            plan_unblock(
                a_retirement_withdrawal(effective_from="446"), manifest_path=manifest
            )

        self.assertIn("needs a block withdrawal", str(caught.exception))

    def test_the_plan_writes_nothing_and_leaves_the_manifest_alone(self):
        manifest = self.manifest_at(BLOCKED)
        before = manifest.read_text(encoding="utf-8")

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        self.assertEqual(manifest.read_text(encoding="utf-8"), before)
        self.assertEqual(plan.document_before, before)
        self.assertTrue(plan.changes_manifest)
        self.assertEqual(self.rows(), [])

    def test_every_byte_outside_the_withdrawn_entry_is_unchanged(self):
        """The round trip is the guard: `serialise` reads and writes one form."""
        manifest = self.manifest_at(BLOCKED)

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        before_doc = json.loads(plan.document_before)
        after_doc = json.loads(plan.document_after)
        before_doc["hooks"][0]["semantic_deps"] = after_doc["hooks"][0]["semantic_deps"]
        self.assertEqual(before_doc, after_doc)
        self.assertEqual(plan.document_after, serialise(after_doc))

    def test_a_missing_manifest_is_an_oserror_the_cli_turns_into_a_refusal(self):
        with self.assertRaises(OSError):
            plan_unblock(a_reversal(), manifest_path=self.tmp / "nothing.json")

    def test_an_endpoint_declared_by_two_hooks_leaves_neither_blocking_it(self):
        """The report must be as wide as the change.

        The loop removes the endpoint from every hook that declares it, and used
        to keep only the LAST matching `hook_id` — so a human confirming the
        withdrawal was told about one hook while two were edited. Latent in the
        shipped manifest, where no pair normalises together, and one edit away
        from being a lie in the output. `hook_ids` and `removed` are tuples now.
        """
        manifest = self.manifest_at(
            [SUBJECT_SLASHED],
            extra=[hook_entry("replace_reels_stream_endpoint", [SUBJECT])],
        )

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        document = json.loads(plan.document_after)
        self.assertEqual([entry["semantic_deps"] for entry in document["hooks"]], [[], []])
        self.assertEqual(
            plan.hook_ids, ("tigon_url_block", "replace_reels_stream_endpoint")
        )
        self.assertEqual(plan.removed, (SUBJECT_SLASHED, SUBJECT))

    def test_a_hook_with_no_semantic_deps_key_at_all_is_skipped_not_crashed(self):
        entry = hook_entry("install_settings_long_click", strategy="ui_attach")
        del entry["semantic_deps"]
        manifest = self.manifest_at(BLOCKED, extra=[entry])

        plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=manifest)

        self.assertEqual(plan.hook_ids, (BLOCK_HOOK,))


class ApplyUnblockTests(ReversalTestCase):
    """The record first, then the manifest, and only with `confirm`.

    The ordering is asserted by breaking the second write rather than by reading
    the source: `rulings.apply` documents the same order for the same reason, and
    a comment claiming an order is not an order.
    """

    def setUp(self) -> None:
        super().setUp()
        self.hooks_json = self.manifest_at(BLOCKED)
        self.plan = plan_unblock(a_reversal(subject=SUBJECT), manifest_path=self.hooks_json)

    def test_a_confirmed_apply_records_the_reversal_and_writes_the_manifest(self):
        written = apply_unblock(self.plan, confirm=True, root=self.tmp)

        self.assertEqual(written, self.reversals)
        self.assertEqual(self.rows(), [a_reversal(subject=SUBJECT).to_dict()])
        self.assertNotIn(SUBJECT, self.deps(self.hooks_json))
        self.assertEqual(len(self.deps(self.hooks_json)), len(BLOCKED) - 1)

    def test_without_confirm_nothing_is_written_at_all(self):
        with self.assertRaises(ReversalError) as caught:
            apply_unblock(self.plan, confirm=False, root=self.tmp)

        self.assertIn("pass confirm", str(caught.exception))
        self.assertIn("changes what the app blocks", str(caught.exception))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.deps(self.hooks_json), list(BLOCKED))

    def test_the_confirm_guard_runs_before_the_manifest_is_even_read(self):
        """A plan against a manifest that has since vanished still refuses on confirm."""
        self.hooks_json.unlink()

        with self.assertRaises(ReversalError) as caught:
            apply_unblock(self.plan, confirm=False, root=self.tmp)

        self.assertIn("pass confirm", str(caught.exception))

    def test_a_manifest_that_moved_since_the_plan_is_refused(self):
        self.hooks_json.write_text(
            self.plan.document_before.replace("robust", "fragile"), encoding="utf-8"
        )

        with self.assertRaises(ReversalError) as caught:
            apply_unblock(self.plan, confirm=True, root=self.tmp)

        self.assertIn("changed since this plan was made", str(caught.exception))
        self.assertIn("nobody reviewed", str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_a_manifest_that_moved_only_in_whitespace_is_still_refused(self):
        """Byte comparison, because a reformat is exactly what must not be overwritten."""
        self.hooks_json.write_text(
            self.plan.document_before + "\n", encoding="utf-8"
        )

        with self.assertRaises(ReversalError):
            apply_unblock(self.plan, confirm=True, root=self.tmp)

    def test_the_reversal_survives_a_manifest_write_that_fails(self):
        """The ordering, proved. The decision is on disk; the manifest is not."""
        before = self.hooks_json.read_text(encoding="utf-8")

        with mock.patch(
            "dfinsta_pipeline.manifest_patch.write_manifest_atomically",
            side_effect=OSError("no space left on device"),
        ):
            with self.assertRaises(OSError):
                apply_unblock(self.plan, confirm=True, root=self.tmp)

        self.assertEqual(self.rows(), [a_reversal(subject=SUBJECT).to_dict()])
        self.assertEqual(self.hooks_json.read_text(encoding="utf-8"), before)

    def test_the_manifest_is_untouched_when_the_record_cannot_be_written(self):
        """The other half of the order: a refused append never reaches the manifest."""
        append(a_reversal(subject=SUBJECT), root=self.tmp)
        before = self.hooks_json.read_text(encoding="utf-8")

        with self.assertRaises(ReversalError) as caught:
            apply_unblock(self.plan, confirm=True, root=self.tmp)

        self.assertIn("was already withdrawn", str(caught.exception))
        self.assertEqual(self.hooks_json.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.rows()), 1)

    def test_apply_honours_a_reversals_path_that_is_not_under_root(self):
        location = self.tmp / "elsewhere" / "rev.jsonl"

        written = apply_unblock(
            self.plan, confirm=True, root=self.tmp, reversals_path=location
        )

        self.assertEqual(written, location)
        self.assertFalse(self.reversals.exists())

    def test_the_written_manifest_is_one_load_manifest_accepts(self):
        """`write_manifest_atomically` validates before it renames; assert the result."""
        from dfinsta_pipeline.hook_manifest import load_manifest

        apply_unblock(self.plan, confirm=True, root=self.tmp)

        hooks = load_manifest(self.hooks_json)
        self.assertEqual([hook.hook_id for hook in hooks], [BLOCK_HOOK])
        self.assertNotIn(SUBJECT, hooks[0].semantic_deps)


# ======================================================================== cli


class CliTests(ReversalTestCase):
    """Every path and every exit code. 0 on success, 2 on a refusal, never 1.

    `main` returning 0 on a refusal is the mutation that survives everything
    else in this file, because every library assertion still holds while the
    interface lies about them.
    """

    def test_list_on_an_empty_store_says_so_and_exits_zero(self):
        code, out, err = self.run_main("list")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("No decision has been withdrawn", out)
        self.assertIn("ordinary state", out)
        self.assertIn("manifest/REVERSALS.md", out)

    def test_list_names_every_row_with_its_author_reason_and_original_decision(self):
        self.write_reversals(
            reversal_row(),
            a_retirement_withdrawal(effective_from="446").to_dict(),
        )

        code, out, err = self.run_main("list")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("RECORDED REVERSALS (2)", out)
        self.assertIn(f"block: {SUBJECT_SLASHED}", out)
        self.assertIn(f"retirement: {DISCOVER} from 446", out)
        self.assertIn(f"withdraws {DOCKET}", out)
        self.assertIn(ARNAV, out)
        self.assertIn(RATIONALE, out)
        self.assertIn("Both rows survive", out)

    def test_list_refuses_a_store_it_cannot_read_and_exits_two(self):
        self.write_reversals("{not json")

        code, out, err = self.run_main("list")

        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("refused: "), err)
        self.assertEqual(out, "")

    def test_list_reads_the_reversals_flag_over_the_root_default(self):
        elsewhere = self.tmp / "elsewhere.jsonl"
        elsewhere.write_text(json.dumps(reversal_row()) + "\n", encoding="utf-8")

        code, out, _ = self.run_main("--reversals", str(elsewhere), "list")

        self.assertEqual(code, 0)
        self.assertIn("RECORDED REVERSALS (1)", out)

    def test_withdraw_block_records_the_row_writes_the_manifest_and_says_both(self):
        manifest = self.manifest_at(BLOCKED)

        code, out, err = self.withdraw_block(manifest)

        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"withdrew {SUBJECT} from {BLOCK_HOOK}.semantic_deps", out)
        self.assertIn(str(self.reversals), out)
        self.assertIn(str(manifest), out)
        self.assertIn("Commit it", out)
        self.assertEqual(len(self.rows()), 1)

    def test_the_recorded_decision_id_is_derived_from_the_answer(self):
        manifest = self.manifest_at(BLOCKED)

        self.withdraw_block(manifest)

        row = self.rows()[0]
        self.assertEqual(
            row["decision_id"],
            reversal_id(
                withdraws="block",
                subject=SUBJECT,
                original_decision_id=DOCKET,
                ruled_by=ARNAV,
                rationale=RATIONALE,
                recorded_at=WHEN,
            ),
        )

    def test_withdraw_block_without_confirm_refuses_and_changes_nothing(self):
        manifest = self.manifest_at(BLOCKED)

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = reversal_module.main(
                [
                    "--root", str(self.tmp),
                    "withdraw-block",
                    "--endpoint", SUBJECT,
                    "--manifest", str(manifest),
                    "--original-decision-id", DOCKET,
                    "--ruled-by", ARNAV,
                    "--rationale", RATIONALE,
                    "--recorded-at", WHEN,
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("pass confirm", stderr.getvalue())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.deps(manifest), list(BLOCKED))

    def test_withdraw_block_on_an_endpoint_nothing_blocks_refuses(self):
        manifest = self.manifest_at(BLOCKED)

        code, _, err = self.withdraw_block(manifest, **{"--endpoint": "feed/nowhere/"})

        self.assertEqual(code, 2)
        self.assertIn("is not in any hook's semantic_deps", err)
        self.assertEqual(self.rows(), [])

    def test_withdraw_block_on_a_missing_manifest_refuses_rather_than_traces(self):
        code, _, err = self.withdraw_block(self.tmp / "gone.json")

        self.assertEqual(code, 2)
        self.assertIn("refused:", err)

    def test_withdraw_block_on_a_non_canonical_manifest_refuses(self):
        manifest = self.manifest_at(BLOCKED, dumper=lambda d: json.dumps(d, indent=4))

        code, _, err = self.withdraw_block(manifest)

        self.assertEqual(code, 2)
        self.assertIn("not in canonical form", err)

    def test_a_second_withdrawal_of_the_same_endpoint_refuses_at_the_manifest(self):
        """The first one removed it, so there is nothing left to withdraw.

        `append`'s duplicate check never fires on this path — `plan_unblock`
        refuses first — and both refusals are correct. The one that fires is the
        one that tells the human what actually happened.
        """
        manifest = self.manifest_at(BLOCKED)
        self.withdraw_block(manifest)

        code, _, err = self.withdraw_block(manifest)

        self.assertEqual(code, 2)
        self.assertIn("is not in any hook's semantic_deps", err)
        self.assertEqual(len(self.rows()), 1)

    def test_withdraw_retirement_records_a_row_and_names_the_version(self):
        code, out, err = self.withdraw_retirement()

        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"{DISCOVER} is expected again from 446", out)
        self.assertIn("Commit it", out)
        self.assertEqual(self.rows()[0]["effective_from"], "446")
        self.assertEqual(self.rows()[0]["withdraws"], "retirement")

    def test_withdraw_retirement_without_confirm_refuses_and_writes_nothing(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = reversal_module.main(
                [
                    "--root", str(self.tmp),
                    "withdraw-retirement",
                    "--hook", DISCOVER,
                    "--from-version", "446",
                    "--original-decision-id", DOCKET,
                    "--ruled-by", ARNAV,
                    "--rationale", RATIONALE,
                    "--recorded-at", WHEN,
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("can fail a future port; pass confirm", stderr.getvalue())
        self.assertEqual(self.rows(), [])

    def test_withdraw_retirement_refuses_a_version_that_is_not_a_number(self):
        code, _, err = self.withdraw_retirement(**{"--from-version": "44a"})

        self.assertEqual(code, 2)
        self.assertIn("needs effective_from", err)
        self.assertEqual(self.rows(), [])

    def test_withdraw_retirement_refuses_a_duplicate_withdrawal(self):
        self.withdraw_retirement()

        code, _, err = self.withdraw_retirement(
            **{"--recorded-at": "2026-08-10T10:00:00Z"}
        )

        self.assertEqual(code, 2)
        self.assertIn("was already withdrawn", err)
        self.assertEqual(len(self.rows()), 1)

    def test_withdraw_retirement_writes_to_the_reversals_flag_when_given_one(self):
        location = self.tmp / "elsewhere.jsonl"

        code, out, _ = self.run_main(
            "--reversals", str(location),
            "withdraw-retirement",
            "--hook", DISCOVER,
            "--from-version", "446",
            "--original-decision-id", DOCKET,
            "--ruled-by", ARNAV,
            "--rationale", RATIONALE,
            "--recorded-at", WHEN,
            "--confirm",
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(self.rows(location)), 1)
        self.assertFalse(self.reversals.exists())

    def test_a_missing_required_flag_is_argparses_refusal_and_not_a_write(self):
        for flag in (
            "--original-decision-id",
            "--ruled-by",
            "--rationale",
            "--recorded-at",
        ):
            with self.subTest(flag=flag):
                arguments = {
                    "--hook": DISCOVER,
                    "--from-version": "446",
                    "--original-decision-id": DOCKET,
                    "--ruled-by": ARNAV,
                    "--rationale": RATIONALE,
                    "--recorded-at": WHEN,
                }
                del arguments[flag]
                flat = [part for pair in arguments.items() for part in pair]
                with self.assertRaises(SystemExit) as caught:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.run_main("withdraw-retirement", *flat, "--confirm")
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.rows(), [])

    def test_no_subcommand_is_refused_by_argparse(self):
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.run_main()

        self.assertEqual(caught.exception.code, 2)

    def test_a_blank_rationale_from_the_command_line_is_refused(self):
        manifest = self.manifest_at(BLOCKED)

        code, _, err = self.withdraw_block(manifest, **{"--rationale": "   "})

        self.assertEqual(code, 2)
        self.assertIn("missing rationale", err)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.deps(manifest), list(BLOCKED))

    def test_the_row_the_cli_writes_is_one_read_reversals_accepts(self):
        """Round trip through the reader that will consume it, as `publish` does."""
        manifest = self.manifest_at(BLOCKED)
        self.withdraw_block(manifest)
        self.withdraw_retirement()

        found = read_reversals(self.tmp)

        self.assertEqual([item.withdraws for item in found], ["block", "retirement"])
        self.assertEqual(found[0].subject, SUBJECT)
        self.assertEqual(found[1].effective_from, "446")


# ============================================================== known defects


class ClosedDefectTests(ReversalTestCase):
    """`expectedFailure` on purpose. Each asserts what the docstrings promise.

    The suite stays green today and reports an *unexpected success* the moment
    one is closed. See this module's docstring for the reasoning behind each.
    """

    def test_a_manifest_with_no_hooks_key_is_refused_rather_than_traced(self):
        """`KeyError` is not `ReversalError`, `ValueError` or `OSError`.

        `plan_unblock` reaches `document["hooks"]` on any JSON object without
        that key, and `main`'s `except` clause does not name `KeyError`, so the
        tool exits 1 with a stack trace instead of `refused:` with exit 2. The
        same shape sits at `entry["hook_id"]`. `tests/test_retirement.py` records
        the identical gap for `TypeError` as its own defect 4.
        """
        path = self.tmp / "manifest" / "empty.json"
        path.write_text(serialise({"schema_version": 1}), encoding="utf-8")

        code, _, err = self.withdraw_block(path)

        self.assertEqual(code, 2)
        self.assertIn("refused:", err)

    def test_root_scopes_the_manifest_as_well_as_the_reversal_store(self):
        """One flag scopes half the operation.

        `--reversals` defaults to `<root>/manifest/reversals.jsonl` and
        `--manifest` defaults to `manifest/hooks.json` resolved against the
        process's working directory. Run from anywhere but the repository root,
        `withdraw-block --root <repo>` records the reversal inside the repository
        and reads the manifest outside it.
        """
        self.manifest_at(BLOCKED)
        somewhere_else = tempfile.TemporaryDirectory()
        self.addCleanup(somewhere_else.cleanup)

        with contextlib.chdir(somewhere_else.name):
            code, _, err = self.run_main(
                "withdraw-block",
                "--endpoint", SUBJECT,
                "--original-decision-id", DOCKET,
                "--ruled-by", ARNAV,
                "--rationale", RATIONALE,
                "--recorded-at", WHEN,
                "--confirm",
            )

        self.assertEqual(code, 0, err)


class ClosedConsumerDefectTests(CorpusTestCase):
    """The consumer that does not go through `retirements_in_force`."""

    def test_the_default_sweep_honours_a_withdrawal(self):
        """`expectation.sweep` reads the raw retirement file and bypasses the store.

        `retirements_in_force` says "every consumer of 'is this hook retired'
        goes through here". `sweep` does not: it calls `read_retirements(root)`
        once and hands it to `compare` as `retirements=`, which then takes the
        `retired_by(version, retirements)` branch with no `withdrawn` map. The
        bare CLI — the mode with no `--version`, which is what a release script
        would run — reports the port as met while `--version 446` reports the
        drop, from the same three files.
        """
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))
        self.write_reversals(
            a_retirement_withdrawal(subject=TIGON, effective_from="446")
        )

        targeted = run_expectation(self.tmp, "--version", "446")
        swept = run_expectation(self.tmp)

        self.assertEqual(targeted[0], 3, targeted)
        self.assertEqual(swept[0], 3, swept)

    def test_the_control_for_the_defect_above_is_that_the_sweep_runs_at_all(self):
        """An `expectedFailure` that failed for an unrelated reason proves nothing.

        Without this, the sweep raising, skipping every pair, or exiting 2 would
        look exactly like the defect being present.
        """
        self.four_ports()
        self.write_retirements(retirement_row(TIGON, effective_from="442"))

        swept = run_expectation(self.tmp)

        self.assertEqual(swept[0], 0, swept)
        self.assertIn("445", swept[1])
        self.assertIn("446", swept[1])


if __name__ == "__main__":
    unittest.main()

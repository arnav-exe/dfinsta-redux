"""Tests for the agent-proposal gate: nothing is believed on a proposer's say-so.

Five of the seven hooks resolve mechanically. The other two — the profile
action-bar settings hooks — have no fingerprint anything in the decode can match,
so an agent has to find them. A blind holdout showed agents *can*: two of three
uncontaminated proposers independently reached `LX/06X7;->AP1`. It also showed the
failure to design against, because one of those proposers justified its (correct)
answer with a fabricated claim that a live listener register was "usually null".

So every assertion here is about refusing to accept a proposal for any reason the
proposer supplied. The three independent checks are re-derivation by a
deterministic validator, agreement measured on *content*, and an adversarial
verifier that never sees the rationale. These tests pin that each one can refuse
alone, and that none of them can be talked out of a refusal.

Almost every test injects a `FakeValidator` returning canned `validate_candidates`
rows. That keeps them fast and independent of `work/`, which is gitignored and
frequently absent. One integration test uses the real validator against the real
430 decode and skips when that decode is not present.

`MutationTests` deletes a guard and shows what ships without it. Each docstring
names the production failure the guard is standing in front of.

`ReportedDefectTests` holds regressions for defects this suite characterised and
the module then fixed. Each docstring records what the defect would have cost in
production, because that is the reason to keep the test rather than the reason it
once failed. Three more of those regressions live with the class a reader would
look in — payload in the fingerprint with `FingerprintTests`, the empty anchor
line with `ProposalValidationTests`, and the mapping-key conflict with
`LoadProposalsTests`. `OpenGapTests` pins the two gaps that are reported and not
yet fixed, both of them about who counts as the proposer of an accepted answer.
"""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.evidence import (
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    Producer,
    Subject,
    Verdict,
)
from dfinsta_pipeline.hook_manifest import (
    Hook,
    HostFingerprint,
    ManifestError,
    load_manifest,
    resolve_in_source,
)
from dfinsta_pipeline.proposals import (
    Assessment,
    Proposal,
    ProposalError,
    Refutation,
    accepted_hosts,
    assess,
    group_by_fingerprint,
    independent_proposers,
    load_proposals,
    one_per_proposer,
    operations_for,
    validate_proposals,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "hooks.json"
RECONSTRUCTION_TOOLS = ROOT / "tools" / "reconstruction"
RESOLVER_TOOLS = ROOT / "tools" / "resolver"
STOCK_430 = ROOT / "work" / "430-clean-build-v2" / "stock-430"

DECODE = Path("/nonexistent-decode")

#: A concrete, already-rendered anchor of the shape a proposer submits: the real
#: five-line action-bar anchor collapsed to the one line that actually attaches.
ANCHOR = (
    "iput-object v13, v1, LX/09rb;->A0H:Landroid/view/View$OnLongClickListener;",
)
PAYLOAD = (
    "    new-instance v13, Lcom/dfinstagram/SettingsWrapper;",
    "    invoke-direct {v13}, Lcom/dfinstagram/SettingsWrapper;-><init>()V",
)


def make_hook(**overrides: object) -> Hook:
    """A minimal by-agent UI hook, standing in for the action-bar hook."""
    fields: dict[str, object] = {
        "hook_id": "install_settings_long_click_actionbar",
        "intent": "open the DFInsta settings dialog by long-pressing Options",
        "tier": "ui",
        "strategy": "ui_attach",
        "semantic_deps": (),
        "hosts": (
            HostFingerprint("by_agent", note="no mechanical fingerprint reaches this"),
        ),
        "anchor": (
            "iput-object <long:reg>, <cfg:reg>, <cfgcls:type>-><lcf:member>:"
            "Landroid/view/View$OnLongClickListener;",
        ),
        "payload": (
            "    new-instance <long>, Lcom/dfinstagram/SettingsWrapper;",
            "    invoke-direct {<long>}, Lcom/dfinstagram/SettingsWrapper;-><init>()V",
        ),
        "marker": "Lcom/dfinstagram/SettingsWrapper;",
        "expected_marker_count": 2,
    }
    fields.update(overrides)
    return Hook(**fields)  # type: ignore[arg-type]


def make_proposal(**overrides: object) -> Proposal:
    """A minimal valid proposal; individual fields overridden per test."""
    fields: dict[str, object] = {
        "hook_id": "install_settings_long_click_actionbar",
        "proposer": "agent-a",
        "descriptor": "LX/06X7;",
        "anchor": ANCHOR,
        "payload": PAYLOAD,
    }
    fields.update(overrides)
    return Proposal(**fields)  # type: ignore[arg-type]


def ok_row(**overrides: Any) -> dict[str, Any]:
    """A `validate_candidates.validate` row for a proposal that checks out.

    Copied field-for-field from a real run of the validator against
    `work/430-clean-build-v2/stock-430`, so the fake cannot drift into a shape the
    real validator never emits.
    """
    row: dict[str, Any] = {
        "descriptor_resolves": True,
        "smali_path": "smali_classes6/X/06X7.smali",
        "anchor_whitespace_clean": True,
        "anchor_occurrences": 1,
        "anchor_matches": True,
        "anchor_unique": True,
        "marker_absent": True,
        "payload_writes": ["v13"],
        "registers_safe": True,
        "registers_note": "no conflicting read found in the following window",
        "verdict": "OK",
    }
    row.update(overrides)
    return row


def broken_row(reason: str, **overrides: Any) -> dict[str, Any]:
    """A row the validator refused, carrying the reason it gives."""
    return ok_row(verdict="BROKEN", reason=reason, **overrides)


class FakeValidator:
    """Stands in for `tools/resolver/validate_candidates.validate`.

    Returns one flat row per operation, chosen by the operation's descriptor, so a
    test can say "this site checks out and that one does not" without a decode.
    """

    def __init__(
        self,
        rows: Mapping[str, Mapping[str, Any]] | None = None,
        default: Mapping[str, Any] | None = None,
    ):
        self.rows = {key: dict(value) for key, value in (rows or {}).items()}
        self.default = dict(default) if default is not None else ok_row()
        self.calls: list[tuple[Path, list[dict[str, Any]]]] = []

    def __call__(
        self, decode: Path, operations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        self.calls.append((decode, [dict(item) for item in operations]))
        out = []
        for operation in operations:
            row = dict(self.rows.get(str(operation.get("descriptor")), self.default))
            row.setdefault("id", operation.get("id"))
            row.setdefault("descriptor", operation.get("descriptor"))
            out.append(row)
        return out


def claims_of(assessment: Assessment, kind: EvidenceKind) -> list[EvidenceClaim]:
    return [claim for claim in assessment.claims if claim.kind is kind]


def only_claim(assessment: Assessment, kind: EvidenceKind) -> EvidenceClaim:
    found = claims_of(assessment, kind)
    assert len(found) == 1, f"expected one {kind.value} claim, got {len(found)}"
    return found[0]


class ProposalValidationTests(unittest.TestCase):
    """A proposal too malformed to check must be refused before anything runs."""

    def test_a_minimal_proposal_is_accepted(self):
        proposal = make_proposal()
        self.assertEqual(proposal.proposer, "agent-a")
        self.assertEqual(proposal.rationale, "")
        self.assertEqual(proposal.evidence, ())

    def test_a_missing_hook_id_is_rejected(self):
        for bad in ("", "   ", "\n"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    make_proposal(hook_id=bad)
                self.assertIn("needs a hook_id", str(caught.exception))

    def test_a_missing_proposer_is_rejected_and_says_why_it_matters(self):
        # Without a proposer id there is no way to tell k answers from one agent
        # apart from k answers from k agents, which is the whole independence check.
        for bad in ("", "   "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    make_proposal(proposer=bad)
                message = str(caught.exception)
                self.assertIn("needs a proposer id", message)
                self.assertIn("independence", message)
                self.assertIn("install_settings_long_click_actionbar", message)

    def test_a_missing_descriptor_is_rejected(self):
        for bad in ("", "  "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    make_proposal(descriptor=bad)
                self.assertIn("needs a host descriptor", str(caught.exception))

    def test_a_missing_anchor_is_rejected(self):
        with self.assertRaises(ProposalError) as caught:
            make_proposal(anchor=())
        self.assertIn("needs an anchor", str(caught.exception))

    def test_a_missing_payload_is_rejected(self):
        with self.assertRaises(ProposalError) as caught:
            make_proposal(payload=())
        self.assertIn("needs a payload", str(caught.exception))

    def test_a_leading_whitespace_anchor_is_rejected_and_names_the_mistake(self):
        """Three real 439 Reels anchors were submitted this way and matched zero lines.

        The applier compares its anchor against `line.strip()`, so a dirty anchor
        line is not "nearly right" — it can never match anything. Caught here, the
        message says that; caught four stages later it reads "anchor not found",
        which sends whoever is on the port hunting a version drift that never
        happened.
        """
        with self.assertRaises(ProposalError) as caught:
            make_proposal(anchor=("    " + ANCHOR[0],))
        message = str(caught.exception)
        self.assertIn("whitespace", message)
        self.assertIn("the applier matches stripped lines", message)
        self.assertIn("silently match nothing", message)
        self.assertIn("install_settings_long_click_actionbar", message)

    def test_a_trailing_whitespace_anchor_is_rejected_too(self):
        # `find_anchors` strips both ends, so trailing whitespace is exactly as
        # unmatchable as leading whitespace even though it is harder to see.
        for dirty in (ANCHOR[0] + " ", ANCHOR[0] + "\t", "\t" + ANCHOR[0]):
            with self.subTest(dirty=repr(dirty)):
                with self.assertRaises(ProposalError) as caught:
                    make_proposal(anchor=(dirty,))
                self.assertIn("silently match nothing", str(caught.exception))

    def test_the_whitespace_check_looks_at_every_anchor_line_not_just_the_first(self):
        with self.assertRaises(ProposalError):
            make_proposal(anchor=(ANCHOR[0], "  const v0, 0x7f134a0e"))

    def test_an_empty_anchor_line_is_rejected_because_it_matches_nothing_either(self):
        """Regression: the whitespace guard used to have a hole exactly its own shape.

        `"" == "".strip()`, so an empty anchor line walked straight past the
        whitespace check. `find_anchors` only ever compares against non-empty
        significant lines, so an anchor carrying one could never match — the very
        failure the guard exists to name, arriving with no diagnosis at all. The
        cost was a build-stage "Anchor mismatch 0 of 1" and a port engineer
        hunting a version drift that never happened, over a blank line.
        """
        for bad in (("", ANCHOR[0]), (ANCHOR[0], ""), (ANCHOR[0], "   "), (ANCHOR[0], "\t")):
            with self.subTest(bad=bad):
                with self.assertRaises(ProposalError) as caught:
                    make_proposal(anchor=bad)
                message = str(caught.exception)
                self.assertIn("empty line", message)
                self.assertIn("matches nothing", message)
                self.assertIn("significant lines only", message)
                self.assertIn("install_settings_long_click_actionbar", message)

    def test_from_dict_round_trips_through_to_dict(self):
        proposal = make_proposal(
            rationale="the drawable and label disambiguate the two iputs",
            evidence=("smali_classes6/X/06X7.smali:412",),
        )
        clone = Proposal.from_dict(proposal.to_dict())
        self.assertEqual(clone, proposal)

    def test_from_dict_requires_the_core_fields(self):
        complete = make_proposal().to_dict()
        for missing in ("hook_id", "proposer", "descriptor", "anchor", "payload"):
            with self.subTest(missing=missing):
                data = {k: v for k, v in complete.items() if k != missing}
                with self.assertRaises(KeyError):
                    Proposal.from_dict(data)

    def test_from_dict_propagates_validation(self):
        data = make_proposal().to_dict()
        data["anchor"] = ["    " + ANCHOR[0]]
        with self.assertRaises(ProposalError):
            Proposal.from_dict(data)

    def test_is_frozen(self):
        proposal = make_proposal()
        with self.assertRaises(Exception):
            proposal.descriptor = "LX/0Di2;"  # type: ignore[misc]


class FingerprintTests(unittest.TestCase):
    """Agreement is counted on content. Prose must not move the number."""

    def test_identical_content_with_completely_different_prose_fingerprints_alike(self):
        """This is the entire basis of agreement counting.

        Two language models asked the same question produce different-sounding
        justifications for the same site as a matter of course. If prose were part
        of the identity, two proposers who genuinely converged would never be
        counted as agreeing and the check would be dead weight.
        """
        first = make_proposal(
            proposer="agent-a",
            rationale=(
                "The label id 0x7f134a0e is the Options string, so this builder is "
                "the profile overflow row."
            ),
            evidence=("smali_classes6/X/06X7.smali:412", "res/values/strings.xml"),
        )
        second = make_proposal(
            proposer="agent-b",
            rationale=(
                "Cross-referenced the MobileConfig selector 0x81099a000034a6; this "
                "is the legacy IgActionBar branch."
            ),
            evidence=("dex-strings.txt", "AP1 caller graph"),
        )
        self.assertNotEqual(first.rationale, second.rationale)
        self.assertNotEqual(first.evidence, second.evidence)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_a_different_anchor_fingerprints_differently(self):
        first = make_proposal()
        second = make_proposal(
            anchor=(
                "iput-object v13, v1, LX/09rb;->A0G:Landroid/view/View$OnClickListener;",
            ),
            rationale=first.rationale,
        )
        self.assertEqual(first.rationale, second.rationale)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_a_different_descriptor_fingerprints_differently(self):
        self.assertNotEqual(
            make_proposal().fingerprint,
            make_proposal(descriptor="LX/0Di2;").fingerprint,
        )

    def test_the_proposer_id_is_not_part_of_the_identity(self):
        # Otherwise every proposal would be its own group and nothing could agree.
        self.assertEqual(
            make_proposal(proposer="agent-a").fingerprint,
            make_proposal(proposer="agent-b").fingerprint,
        )

    def test_anchor_line_order_is_part_of_the_identity(self):
        two_line = (ANCHOR[0], "const v0, 0x7f134a0e")
        self.assertNotEqual(
            make_proposal(anchor=two_line).fingerprint,
            make_proposal(anchor=tuple(reversed(two_line))).fingerprint,
        )

    def test_the_fingerprint_is_the_canonical_hash_of_descriptor_anchor_and_payload(self):
        proposal = make_proposal(
            rationale="anything at all", evidence=("and any citation at all",)
        )
        self.assertEqual(
            proposal.fingerprint,
            canonical_sha256(
                {
                    "descriptor": proposal.descriptor,
                    "anchor": list(proposal.anchor),
                    "payload": list(proposal.payload),
                }
            ),
        )
        # The two fields that are *not* in it, pinned against the same proposal.
        self.assertEqual(proposal.fingerprint, make_proposal().fingerprint)

    def test_a_differing_payload_changes_the_fingerprint(self):
        """Regression: the instructions are part of the identity, not just the site.

        `fingerprint` used to cover descriptor and anchor only, so two proposers
        who picked the same anchor and wrote different smali counted as agreeing —
        and which payload actually shipped was decided by list order, because
        `assess` takes `candidates[0]`. Agreement about a site is not agreement
        about what to inject there. The cost was a build carrying instructions
        nobody corroborated, reported as "2 independent proposers agreed".
        """
        clobbering = ("    const-string v1, \"clobbered\"", *PAYLOAD)
        self.assertNotEqual(make_proposal().payload, clobbering)
        self.assertNotEqual(
            make_proposal().fingerprint,
            make_proposal(payload=clobbering).fingerprint,
        )
        # And it is the content, not the length: reordering the same two lines is
        # a different set of instructions too.
        self.assertNotEqual(
            make_proposal().fingerprint,
            make_proposal(payload=tuple(reversed(PAYLOAD))).fingerprint,
        )

    def test_two_proposers_differing_only_in_payload_do_not_agree(self):
        """The consequence the fingerprint change exists for, at the gate.

        Same site, two different injections, and nothing to choose between them:
        that is a disagreement a human has to settle, not a majority of one.
        """
        proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(
                proposer="agent-b",
                payload=("    const-string v1, \"clobbered\"", *PAYLOAD),
            ),
        ]
        self.assertEqual(len(group_by_fingerprint(proposals)), 2)
        assessment = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIn("1 of 2 distinct proposers", assessment.reason)


class ProposalKeyTests(unittest.TestCase):
    """`Proposal.key` identifies one answer, not one author."""

    def test_the_key_is_the_proposer_and_a_fingerprint_prefix(self):
        proposal = make_proposal()
        self.assertEqual(proposal.key, f"agent-a#{proposal.fingerprint[:12]}")

    def test_one_proposer_with_two_answers_gets_two_keys(self):
        # The whole point: validation rows keyed by proposer alone lose one.
        first = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        second = make_proposal(proposer="agent-a", descriptor="LX/0Di2;")
        self.assertNotEqual(first.key, second.key)
        self.assertTrue(first.key.startswith("agent-a#"))
        self.assertTrue(second.key.startswith("agent-a#"))

    def test_two_proposers_with_one_answer_get_two_keys(self):
        first = make_proposal(proposer="agent-a")
        second = make_proposal(proposer="agent-b")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.key, second.key)

    def test_the_key_ignores_prose_exactly_as_the_fingerprint_does(self):
        self.assertEqual(
            make_proposal(rationale="one story", evidence=("a",)).key,
            make_proposal(rationale="another story entirely").key,
        )


class GroupByFingerprintTests(unittest.TestCase):
    def test_buckets_matching_answers_together(self):
        a = make_proposal(proposer="agent-a")
        b = make_proposal(proposer="agent-b", rationale="different story, same site")
        c = make_proposal(proposer="agent-c", descriptor="LX/0Di2;")
        groups = group_by_fingerprint([a, b, c])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[a.fingerprint], [a, b])
        self.assertEqual(groups[c.fingerprint], [c])

    def test_preserves_submission_order_within_a_bucket(self):
        a = make_proposal(proposer="agent-a")
        b = make_proposal(proposer="agent-b")
        self.assertEqual(group_by_fingerprint([b, a])[a.fingerprint], [b, a])

    def test_an_empty_sequence_gives_no_groups(self):
        self.assertEqual(group_by_fingerprint([]), {})

    def test_every_distinct_answer_gets_its_own_bucket(self):
        proposals = [
            make_proposal(proposer=f"agent-{i}", descriptor=f"LX/{i:04d};")
            for i in range(3)
        ]
        self.assertEqual(len(group_by_fingerprint(proposals)), 3)


class IndependentProposersTests(unittest.TestCase):
    def test_distinct_proposers_are_independent(self):
        self.assertTrue(
            independent_proposers(
                [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")]
            )
        )

    def test_a_repeated_proposer_is_not_independent(self):
        # k answers from one agent is one answer. Counting it as k is how a single
        # confidently-wrong agent manufactures consensus by being run repeatedly.
        self.assertFalse(
            independent_proposers(
                [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-a")]
            )
        )

    def test_one_proposal_is_trivially_independent(self):
        self.assertTrue(independent_proposers([make_proposal()]))

    def test_an_empty_sequence_is_trivially_independent(self):
        self.assertTrue(independent_proposers([]))

    def test_independence_is_about_the_proposer_not_the_answer(self):
        # Same agent, two different answers: still one voice.
        self.assertFalse(
            independent_proposers(
                [
                    make_proposal(proposer="agent-a", descriptor="LX/06X7;"),
                    make_proposal(proposer="agent-a", descriptor="LX/0Di2;"),
                ]
            )
        )


class OnePerProposerTests(unittest.TestCase):
    """The collapse that turns k submissions into k votes, once per voice."""

    def test_a_repeated_proposer_contributes_its_first_answer_only(self):
        first = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        second = make_proposal(proposer="agent-a", descriptor="LX/0Di2;")
        third = make_proposal(proposer="agent-b", descriptor="LX/0Di2;")
        self.assertEqual(one_per_proposer([first, second, third]), [first, third])

    def test_distinct_proposers_pass_through_unchanged(self):
        proposals = [
            make_proposal(proposer=f"agent-{name}") for name in ("a", "b", "c")
        ]
        self.assertEqual(one_per_proposer(proposals), proposals)

    def test_submission_order_is_preserved(self):
        # `assess` takes `winner_group[0]`, so this ordering decides which of two
        # identical answers is the one recorded as accepted.
        b = make_proposal(proposer="agent-b")
        a = make_proposal(proposer="agent-a")
        self.assertEqual(one_per_proposer([b, a]), [b, a])

    def test_an_empty_sequence_gives_no_votes(self):
        self.assertEqual(one_per_proposer([]), [])

    def test_the_result_is_a_new_list_not_the_input(self):
        proposals = [make_proposal()]
        self.assertIsNot(one_per_proposer(proposals), proposals)


class ValidateProposalsTests(unittest.TestCase):
    def test_each_proposal_is_re_derived_as_an_applier_shaped_operation(self):
        hook = make_hook()
        validator = FakeValidator()
        proposal = make_proposal()
        results = validate_proposals(hook, [proposal], DECODE, validator)
        self.assertEqual(results[proposal.key]["verdict"], "OK")
        self.assertEqual(results[proposal.key]["proposer"], "agent-a")
        (decode, operations), = validator.calls
        self.assertEqual(decode, DECODE)
        self.assertEqual(
            operations[0],
            {
                "id": hook.hook_id,
                "descriptor": "LX/06X7;",
                "mode": hook.mode,
                "anchor": list(ANCHOR),
                "expected_anchor_count": hook.expected_anchor_count,
                "marker": hook.marker,
                "expected_marker_count": hook.expected_marker_count,
                "payload": list(PAYLOAD),
            },
        )

    def test_a_validator_returning_nothing_is_a_broken_verdict(self):
        # Silence is not consent: an empty result must not read as a clean run.
        def silent(decode: Path, operations: Sequence[Mapping[str, Any]]) -> list[dict]:
            return []

        proposal = make_proposal()
        results = validate_proposals(make_hook(), [proposal], DECODE, silent)
        self.assertEqual(results[proposal.key]["verdict"], "BROKEN")
        self.assertIn("returned no result", results[proposal.key]["reason"])
        # The synthesised row still says whose proposal it belongs to, or the
        # claims built from it could not name a proposer.
        self.assertEqual(results[proposal.key]["proposer"], "agent-a")

    def test_a_validator_that_raises_is_a_failed_check_not_a_crash(self):
        """A validator blowing up must never propagate into the caller.

        The validator walks a multi-gigabyte decode; a duplicate descriptor or an
        unreadable file raises. Letting that escape would abort the whole Resolve
        stage, and the tempting fix — catching it at the top and carrying on —
        would lose the fact that this proposal was never actually checked.
        """

        def explodes(decode: Path, operations: Sequence[Mapping[str, Any]]) -> list[dict]:
            raise ValueError("Duplicate descriptor LX/06X7;")

        proposal = make_proposal()
        results = validate_proposals(make_hook(), [proposal], DECODE, explodes)
        row = results[proposal.key]
        self.assertEqual(row["verdict"], "BROKEN")
        self.assertEqual(row["proposer"], "agent-a")
        self.assertIn("validator raised ValueError", row["reason"])
        self.assertIn("Duplicate descriptor LX/06X7;", row["reason"])

    def test_a_raising_validator_leaves_the_hook_unresolved_rather_than_erroring(self):
        def explodes(decode: Path, operations: Sequence[Mapping[str, Any]]) -> list[dict]:
            raise RuntimeError("decode disappeared mid-run")

        assessment = assess(
            make_hook(),
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            explodes,
        )
        self.assertFalse(assessment.resolved)
        self.assertIn("validator raised RuntimeError", assessment.reason)
        self.assertIn("decode disappeared mid-run", assessment.reason)

    def test_a_raising_validator_produces_inconclusive_claims_not_passing_ones(self):
        # A check that could not run must not read as a check that succeeded.
        def explodes(decode: Path, operations: Sequence[Mapping[str, Any]]) -> list[dict]:
            raise RuntimeError("boom")

        assessment = assess(make_hook(), [make_proposal()], DECODE, explodes)
        for kind in (EvidenceKind.ANCHOR_UNIQUE, EvidenceKind.REGISTERS_SAFE):
            with self.subTest(kind=kind.value):
                self.assertIs(only_claim(assessment, kind).verdict, Verdict.INCONCLUSIVE)

    def test_results_are_keyed_by_the_proposal_not_by_its_author(self):
        validator = FakeValidator(
            {"LX/06X7;": ok_row(), "LX/0Di2;": broken_row("descriptor not found")}
        )
        good = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        bad = make_proposal(proposer="agent-b", descriptor="LX/0Di2;")
        results = validate_proposals(make_hook(), [good, bad], DECODE, validator)
        self.assertEqual(set(results), {good.key, bad.key})
        self.assertEqual(results[good.key]["verdict"], "OK")
        self.assertEqual(results[bad.key]["verdict"], "BROKEN")
        # The author is still recoverable, it just is not the identity.
        self.assertEqual(
            {row["proposer"] for row in results.values()}, {"agent-a", "agent-b"}
        )

    def test_one_proposer_with_two_answers_keeps_both_rows(self):
        """Keying by proposer kept only the last row and both answers inherited it.

        A validator verdict that belongs to one proposal must never be read as the
        verdict for a different proposal, however similar the authorship. See
        `ReportedDefectTests` for what that cost at the gate.
        """
        validator = FakeValidator(
            {
                "LX/06X7;": ok_row(),
                "LX/BAD;": broken_row("descriptor not found in this decode"),
            }
        )
        first = make_proposal(proposer="agent-a", descriptor="LX/BAD;")
        second = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        results = validate_proposals(make_hook(), [first, second], DECODE, validator)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[first.key]["verdict"], "BROKEN")
        self.assertEqual(results[second.key]["verdict"], "OK")

    def test_the_same_answer_submitted_twice_is_one_row(self):
        # Identical content from the same proposer is genuinely one proposal, so
        # keying it once is right rather than lossy.
        validator = FakeValidator()
        twice = [make_proposal(), make_proposal()]
        results = validate_proposals(make_hook(), twice, DECODE, validator)
        self.assertEqual(list(results), [twice[0].key])


class AssessDecisionTests(unittest.TestCase):
    """One test per way the gate can refuse, and one for the way it can accept."""

    def setUp(self):
        self.hook = make_hook()

    def test_no_proposals_at_all_is_not_resolved_and_says_so(self):
        assessment = assess(self.hook, [], DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("no proposals were produced", assessment.reason)
        self.assertIn(self.hook.hook_id, assessment.reason)
        self.assertEqual(assessment.proposals, ())
        self.assertEqual(assessment.claims, ())

    def test_no_proposal_passing_the_validator_is_not_resolved(self):
        """Two proposers agreeing on a site that does not check out is still nothing.

        The reason has to carry the validator's own words, because "no proposal
        survived" tells a human at the gate nothing about whether the descriptor
        vanished, the anchor moved, or the class was already patched. It names the
        proposal rather than the proposer, so a proposer with two answers has both
        of its verdicts spelled out instead of one of them silently standing in.
        """
        validator = FakeValidator(
            {
                "LX/06X7;": broken_row("anchor matched 0 times", anchor_matches=False),
                "LX/0Di2;": broken_row(
                    "descriptor not found in this decode", descriptor_resolves=False
                ),
            }
        )
        first = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        second = make_proposal(proposer="agent-b", descriptor="LX/0Di2;")
        assessment = assess(self.hook, [first, second], DECODE, validator)
        self.assertFalse(assessment.resolved)
        self.assertIn("no proposal survived the deterministic validator", assessment.reason)
        self.assertIn(f"{first.key}: anchor matched 0 times", assessment.reason)
        self.assertIn(
            f"{second.key}: descriptor not found in this decode", assessment.reason
        )
        self.assertIn("agent-a", assessment.reason)
        self.assertIn("agent-b", assessment.reason)

    def test_a_refuted_proposal_is_not_resolved_even_when_everything_else_passed(self):
        """The verifier is asked to refute, and a refutation is decisive on its own.

        It is the only check with eyes on semantics: the validator cannot tell that
        an anchor sits in the follow-button branch rather than the Options branch,
        and agreement cannot either if both proposers made the same reading error.
        """
        proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(proposer="agent-b"),
        ]
        refutation = Refutation(
            self.hook.hook_id,
            "verifier-x",
            True,
            "v13 holds a live listener at this point; the payload overwrites it",
            checked=("register liveness", "caller graph"),
        )
        assessment = assess(
            self.hook, proposals, DECODE, FakeValidator(), refutations=[refutation]
        )
        self.assertFalse(assessment.resolved)
        self.assertIn("a verifier refuted the proposal", assessment.reason)
        self.assertIn("verifier-x", assessment.reason)
        self.assertIn("v13 holds a live listener", assessment.reason)
        claim = only_claim(assessment, EvidenceKind.ADVERSARIAL_VERIFIED)
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertIs(claim.producer, Producer.VERIFIER_AGENT)
        self.assertEqual(claim.actor, "verifier-x")
        self.assertEqual(claim.detail["checked"], ["register liveness", "caller graph"])

    def test_a_verifier_that_looked_and_found_nothing_does_not_block(self):
        assessment = assess(
            self.hook,
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    self.hook.hook_id,
                    "verifier-x",
                    False,
                    "walked every read of v13 after the anchor; found no live use",
                )
            ],
        )
        self.assertTrue(assessment.resolved)
        self.assertIs(
            only_claim(assessment, EvidenceKind.ADVERSARIAL_VERIFIED).verdict,
            Verdict.PASSED,
        )

    def test_three_proposers_with_three_answers_is_ambiguity_for_a_gate(self):
        """Nothing here breaks a tie by ranking. Disagreement is the finding.

        The holdout that justified this module had two of three proposers converge.
        Three answers from three proposers is the opposite result, and the honest
        report is the count, not a winner picked out of a hat.
        """
        proposals = [
            make_proposal(proposer=f"agent-{name}", descriptor=descriptor)
            for name, descriptor in (("a", "LX/06X7;"), ("b", "LX/0Di2;"), ("c", "LX/09rb;"))
        ]
        assessment = assess(self.hook, proposals, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("1 of 3 distinct proposers", assessment.reason)
        self.assertIn("3 distinct answers overall", assessment.reason)
        self.assertIn("genuine ambiguity", assessment.reason)
        self.assertIn("belongs at a gate", assessment.reason)
        self.assertIn("rather than being broken by ranking", assessment.reason)

    def test_the_agreed_answer_failing_the_validator_beats_the_agreement(self):
        """Agreement must never override a failed mechanical check.

        Two proposers reading the same decode make the same mistake all the time —
        they share a training distribution and a prompt. The deterministic checker
        is the only party re-deriving the fact from the bytes, so when the popular
        answer does not check out, the unpopular one that does is *not* promoted
        either. Both outcomes are escalations.
        """
        validator = FakeValidator(
            {
                "LX/06X7;": broken_row("anchor matched 2 times, expected 1", anchor_unique=False),
                "LX/0Di2;": ok_row(),
            }
        )
        proposals = [
            make_proposal(proposer="agent-a", descriptor="LX/06X7;"),
            make_proposal(proposer="agent-b", descriptor="LX/06X7;"),
            make_proposal(proposer="agent-c", descriptor="LX/0Di2;"),
        ]
        assessment = assess(self.hook, proposals, DECODE, validator)
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("the agreed answer", assessment.reason)
        self.assertIn("failed the deterministic validator", assessment.reason)
        self.assertIn("Agreement is not evidence", assessment.reason)
        # The minority answer passed the validator and is still not accepted.
        self.assertEqual(assessment.validations[proposals[2].key]["verdict"], "OK")

    def test_a_majority_that_checks_out_and_is_not_refuted_is_accepted(self):
        validator = FakeValidator(
            {"LX/06X7;": ok_row(), "LX/0Di2;": broken_row("descriptor not found")}
        )
        winner = make_proposal(proposer="agent-a", rationale="options row builder")
        proposals = [
            winner,
            make_proposal(proposer="agent-b", rationale="legacy IgActionBar branch"),
            make_proposal(proposer="agent-c", descriptor="LX/0Di2;"),
        ]
        assessment = assess(self.hook, proposals, DECODE, validator)
        self.assertTrue(assessment.resolved)
        self.assertEqual(assessment.accepted, winner)
        self.assertEqual(assessment.proposals, tuple(proposals))
        self.assertIn("2 independent proposers agreed on LX/06X7;", assessment.reason)
        self.assertIn("passed the validator", assessment.reason)
        self.assertIn("no verifier refuted it", assessment.reason)

    def test_a_single_proposer_never_reaches_agreement_on_its_own(self):
        # One answer is not corroboration however clean it looks.
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIn("only one proposer answered", assessment.reason)
        self.assertIn("nothing to corroborate it", assessment.reason)

    def test_every_escalation_still_carries_every_proposal_and_claim(self):
        # The gate needs the disagreement, not just the verdict.
        proposals = [
            make_proposal(proposer="agent-a", descriptor="LX/06X7;"),
            make_proposal(proposer="agent-b", descriptor="LX/0Di2;"),
        ]
        assessment = assess(self.hook, proposals, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertEqual(assessment.proposals, tuple(proposals))
        self.assertEqual(
            set(assessment.validations), {item.key for item in proposals}
        )
        self.assertEqual(len(claims_of(assessment, EvidenceKind.ANCHOR_UNIQUE)), 2)

    def test_the_assessment_serialises_for_an_escalation_report(self):
        assessment = assess(
            self.hook,
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
        )
        data = assessment.to_dict()
        json.dumps(data)
        self.assertTrue(data["resolved"])
        self.assertEqual(data["accepted"]["proposer"], "agent-a")
        self.assertEqual(len(data["proposals"]), 2)


class FakeConsensusTests(unittest.TestCase):
    """One agent run three times is one answer, not three."""

    def test_one_proposer_repeating_itself_collapses_to_a_single_vote(self):
        """The cheapest attack on an agreement check is to re-run the same agent.

        The holdout's dangerous proposer was fluent and confident and wrong about
        its justification; run it three times and it says the same thing three
        times. If repetition counted, that alone would clear the agreement gate.
        """
        proposals = [make_proposal(proposer="agent-a") for _ in range(3)]
        assessment = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("only one proposer answered", assessment.reason)
        # And the claim the ledger keeps says the same thing as the decision.
        claim = only_claim(assessment, EvidenceKind.PROPOSER_AGREEMENT)
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.detail["proposals"], 1)

    def test_two_of_the_three_being_one_agent_still_leaves_a_single_voice(self):
        proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(proposer="agent-a", rationale="restated"),
            make_proposal(proposer="agent-b", descriptor="LX/0Di2;"),
        ]
        assessment = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)
        self.assertIn("1 of 2 distinct proposers", assessment.reason)

    def test_a_genuine_second_proposer_is_what_flips_it(self):
        # Same three answers, one of them relabelled to a second agent.
        proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(proposer="agent-b", rationale="restated"),
            make_proposal(proposer="agent-c", descriptor="LX/0Di2;"),
        ]
        assessment = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertTrue(assessment.resolved)


class ClaimEmissionTests(unittest.TestCase):
    """Every proposal produces evidence from a producer that is not the proposer."""

    def setUp(self):
        self.hook = make_hook()

    def test_assess_emits_one_agreement_claim_and_two_claims_per_proposal(self):
        proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(proposer="agent-b"),
            make_proposal(proposer="agent-c", descriptor="LX/0Di2;"),
        ]
        assessment = assess(self.hook, proposals, DECODE, FakeValidator())
        agreement = only_claim(assessment, EvidenceKind.PROPOSER_AGREEMENT)
        self.assertIs(agreement.producer, Producer.STATISTICS)
        self.assertEqual(len(claims_of(assessment, EvidenceKind.ANCHOR_UNIQUE)), 3)
        self.assertEqual(len(claims_of(assessment, EvidenceKind.REGISTERS_SAFE)), 3)
        for claim in assessment.claims:
            with self.subTest(kind=claim.kind.value):
                self.assertEqual(claim.hook_id, self.hook.hook_id)

    def test_the_deterministic_claims_name_the_validator_as_their_actor(self):
        # Not the proposer: the ledger refuses a claim whose actor proposed the hook.
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator())
        for kind in (EvidenceKind.ANCHOR_UNIQUE, EvidenceKind.REGISTERS_SAFE):
            with self.subTest(kind=kind.value):
                claim = only_claim(assessment, kind)
                self.assertEqual(claim.actor, "tools/resolver/validate_candidates.py")
                self.assertIs(claim.producer, Producer.DETERMINISTIC)
                self.assertNotEqual(claim.actor, "agent-a")

    def test_a_clean_row_maps_to_a_passed_anchor_claim(self):
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator())
        claim = only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("smali_classes6/X/06X7.smali", claim.summary)
        self.assertIn("matches exactly 1 time(s)", claim.summary)
        self.assertIn("no marker present", claim.summary)
        self.assertEqual(claim.detail["proposer"], "agent-a")

    def test_each_failed_anchor_check_is_named_in_the_summary(self):
        checks = (
            "descriptor_resolves",
            "anchor_whitespace_clean",
            "anchor_matches",
            "anchor_unique",
        )
        for check in checks:
            with self.subTest(check=check):
                validator = FakeValidator(
                    default=broken_row("the validator's own words", **{check: False})
                )
                assessment = assess(self.hook, [make_proposal()], DECODE, validator)
                claim = only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE)
                self.assertIs(claim.verdict, Verdict.FAILED)
                self.assertIn(check, claim.summary)
                self.assertIn("agent-a's proposal failed", claim.summary)
                self.assertIn("the validator's own words", claim.summary)

    def test_several_failed_checks_are_all_named(self):
        validator = FakeValidator(
            default=broken_row("nope", anchor_matches=False, anchor_unique=False)
        )
        assessment = assess(self.hook, [make_proposal()], DECODE, validator)
        summary = only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE).summary
        self.assertIn("anchor_matches", summary)
        self.assertIn("anchor_unique", summary)

    def test_whitespace_uncleanliness_is_named_separately_from_a_zero_match(self):
        """The validator's own `ok` never mentions whitespace; the claim must.

        A dirty anchor already shows up as `anchor_matches=False`, so the validator
        reports "anchor matched 0 times" and a human goes looking for a moved line.
        Naming `anchor_whitespace_clean` puts the actual cause at the gate.
        """
        validator = FakeValidator(
            default=broken_row(
                "anchor matched 0 times", anchor_whitespace_clean=False, anchor_matches=False
            )
        )
        assessment = assess(self.hook, [make_proposal()], DECODE, validator)
        self.assertIn(
            "anchor_whitespace_clean",
            only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE).summary,
        )

    def test_a_present_marker_fails_the_anchor_claim(self):
        validator = FakeValidator(
            default=broken_row("marker already present (partially applied?)", marker_absent=False)
        )
        assessment = assess(self.hook, [make_proposal()], DECODE, validator)
        claim = only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE)
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertIn("marker_absent", claim.summary)

    def test_a_missing_check_key_is_inconclusive_not_passed(self):
        """Absence is never a pass — the ledger's founding rule.

        A validator that grew a new early-return, or an older result file replayed
        against newer code, produces rows with keys simply missing. Treating a key
        that is not there as "did not report False" would quietly certify a check
        that never ran.
        """
        for check in (
            "descriptor_resolves",
            "anchor_whitespace_clean",
            "anchor_matches",
            "anchor_unique",
        ):
            with self.subTest(missing=check):
                row = ok_row()
                del row[check]
                assessment = assess(
                    self.hook, [make_proposal()], DECODE, FakeValidator(default=row)
                )
                claim = only_claim(assessment, EvidenceKind.ANCHOR_UNIQUE)
                self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
                self.assertIn("validator did not report", claim.summary)
                self.assertIn(check, claim.summary)

    def test_registers_safe_true_passes_and_carries_the_note(self):
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator())
        claim = only_claim(assessment, EvidenceKind.REGISTERS_SAFE)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("no conflicting read found", claim.summary)
        self.assertIn("agent-a", claim.summary)

    def test_registers_safe_false_fails_and_carries_the_note(self):
        validator = FakeValidator(
            default=broken_row(
                "v13 is read at 'invoke-virtual {v13}, LX/09rb;->A00()V' before being rewritten",
                registers_safe=False,
                registers_note=(
                    "v13 is read at 'invoke-virtual {v13}, LX/09rb;->A00()V' "
                    "before being rewritten"
                ),
            )
        )
        assessment = assess(self.hook, [make_proposal()], DECODE, validator)
        claim = only_claim(assessment, EvidenceKind.REGISTERS_SAFE)
        self.assertIs(claim.verdict, Verdict.FAILED)
        self.assertIn("before being rewritten", claim.summary)

    def test_registers_safe_none_is_inconclusive_which_is_stricter_than_the_validator(self):
        """Deliberately stricter than `validate_candidates`' own `ok`.

        That `ok` is `... and row["registers_safe"] is not False`, so a `None` — the
        value the validator writes when the anchor did not match and liveness was
        never evaluated — counts towards its verdict. The ledger cannot afford that
        equivalence: `passed` means the phone or the checker demonstrated the fact,
        and a check that did not run demonstrated nothing. This is the exact shape
        of the holdout failure, where a register's state was *asserted* rather than
        established.
        """
        row = ok_row(registers_safe=None, registers_note="not evaluated: anchor did not match")
        # The validator's own rule, transcribed from validate_candidates.py:123-129.
        validator_ok = (
            row["descriptor_resolves"]
            and row["anchor_matches"]
            and row["anchor_unique"]
            and (row["marker_absent"] in (True, None))
            and row["registers_safe"] is not False
        )
        self.assertTrue(validator_ok, "the validator itself would call this row OK")

        assessment = assess(
            self.hook, [make_proposal()], DECODE, FakeValidator(default=row)
        )
        claim = only_claim(assessment, EvidenceKind.REGISTERS_SAFE)
        self.assertIs(claim.verdict, Verdict.INCONCLUSIVE)
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertFalse(claim.verdict.satisfies)
        self.assertIn("register liveness was not evaluated", claim.summary)
        self.assertIn("not evaluated: anchor did not match", claim.summary)

    def test_a_missing_registers_safe_key_is_also_inconclusive(self):
        row = ok_row()
        del row["registers_safe"]
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator(default=row))
        self.assertIs(
            only_claim(assessment, EvidenceKind.REGISTERS_SAFE).verdict,
            Verdict.INCONCLUSIVE,
        )

    def test_the_claim_detail_carries_the_row_it_was_derived_from(self):
        assessment = assess(self.hook, [make_proposal()], DECODE, FakeValidator())
        claim = only_claim(assessment, EvidenceKind.REGISTERS_SAFE)
        self.assertEqual(claim.detail["row"]["anchor_occurrences"], 1)
        self.assertNotIn("id", claim.detail["row"])


class LedgerIntegrationTests(unittest.TestCase):
    """The ledger is the control; `assess` must feed it, not bypass it."""

    def setUp(self):
        self.hook = make_hook()
        self.ledger = EvidenceLedger()
        self.proposals = [
            make_proposal(proposer="agent-a"),
            make_proposal(proposer="agent-b"),
        ]

    def test_the_subject_is_registered_as_agent_provenance_with_the_winner(self):
        assessment = assess(
            self.hook, self.proposals, DECODE, FakeValidator(), ledger=self.ledger
        )
        self.assertTrue(assessment.resolved)
        readiness = self.ledger.readiness(self.hook.hook_id)
        # `agent` provenance demands all seven kinds; `mechanical` would demand five.
        self.assertEqual(len(readiness.statuses), len(EvidenceKind))

    def test_every_claim_reaches_the_ledger(self):
        assessment = assess(
            self.hook, self.proposals, DECODE, FakeValidator(), ledger=self.ledger
        )
        self.assertEqual(self.ledger.claims, assessment.claims)
        self.assertEqual(
            len(self.ledger.claims_for(self.hook.hook_id, EvidenceKind.ANCHOR_UNIQUE)), 2
        )

    def test_claims_are_recorded_even_when_the_hook_is_not_resolved(self):
        # An escalation still has to arrive at the gate with its evidence attached.
        assess(
            self.hook,
            [make_proposal(proposer="agent-a", descriptor="LX/0Di2;")],
            DECODE,
            FakeValidator(),
            ledger=self.ledger,
        )
        self.assertTrue(self.ledger.claims)

    def test_no_claim_assess_emits_is_produced_by_the_winning_proposer(self):
        """Self-attestation is a schema error here, so this must hold by construction.

        The producers `assess` uses are a tool path and a statistics actor; the only
        agent-shaped actor is the verifier, which the caller supplies. If any of
        them ever collided with the proposer id, `EvidenceLedger.record` would
        refuse and the whole assessment would raise rather than quietly accept.
        """
        assessment = assess(
            self.hook,
            self.proposals,
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(self.hook.hook_id, "verifier-x", False, "found nothing")
            ],
            ledger=self.ledger,
        )
        assert assessment.accepted is not None
        proposer = assessment.accepted.proposer
        actors = {claim.actor for claim in assessment.claims}
        self.assertEqual(
            actors,
            {
                "tools/resolver/validate_candidates.py",
                "resolve.proposer_agreement",
                "verifier-x",
            },
        )
        self.assertNotIn(proposer, actors)
        for other in {item.proposer for item in self.proposals}:
            with self.subTest(proposer=other):
                self.assertNotIn(other, actors)
        # And the rule is enforced, not merely respected: the same claim relabelled
        # with the proposer's own id is refused at record time.
        forged = replace(assessment.claims[0], actor=proposer)
        with self.assertRaises(EvidenceError) as caught:
            self.ledger.record(forged)
        self.assertIn("may not also produce its evidence", str(caught.exception))

    def test_a_verifier_using_the_proposers_id_never_becomes_evidence(self):
        """An agent cannot refute — or clear — its own proposal.

        `assess` separates such a refutation out before building any claim, so the
        run ends in a stated escalation instead of an exception, and nothing the
        proposer said reaches the ledger. The ledger's own refusal is still the
        backstop, proved below on a hand-built claim.
        """
        assessment = assess(
            self.hook,
            self.proposals,
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(self.hook.hook_id, "agent-a", False, "looks fine to me")
            ],
            ledger=self.ledger,
        )
        self.assertFalse(assessment.resolved)
        self.assertIn("agent-a", assessment.reason)
        self.assertIn("not independent evidence", assessment.reason)
        self.assertEqual(claims_of(assessment, EvidenceKind.ADVERSARIAL_VERIFIED), [])
        self.assertNotIn("agent-a", {claim.actor for claim in self.ledger.claims})
        # The backstop is still armed: the same claim, recorded by hand, is refused.
        with self.assertRaises(EvidenceError) as caught:
            self.ledger.record(
                EvidenceClaim(
                    hook_id=self.hook.hook_id,
                    kind=EvidenceKind.ADVERSARIAL_VERIFIED,
                    verdict=Verdict.PASSED,
                    producer=Producer.VERIFIER_AGENT,
                    actor="agent-a",
                    summary="looks fine to me",
                )
            )
        self.assertIn("agent-a", str(caught.exception))
        self.assertIn("may not also produce its evidence", str(caught.exception))

    def test_a_third_party_verifiers_claim_still_reaches_the_ledger(self):
        # The separation must be narrow enough to leave the check usable: an
        # outside verifier's finding is evidence and is recorded as such.
        assessment = assess(
            self.hook,
            self.proposals,
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(self.hook.hook_id, "verifier-x", False, "found no live use of v13")
            ],
            ledger=self.ledger,
        )
        self.assertTrue(assessment.resolved)
        claim = only_claim(assessment, EvidenceKind.ADVERSARIAL_VERIFIED)
        self.assertEqual(claim.actor, "verifier-x")
        self.assertIn(claim, self.ledger.claims)

    def test_the_hook_is_not_ready_on_the_static_evidence_alone(self):
        # Three of the seven kinds only a device can produce. Passing the static
        # checks must not be enough to advance; the 430 settings hook did exactly
        # that and was inert at runtime.
        assess(self.hook, self.proposals, DECODE, FakeValidator(), ledger=self.ledger)
        readiness = self.ledger.readiness(self.hook.hook_id)
        self.assertFalse(readiness.ready)
        self.assertEqual(
            set(readiness.missing),
            {
                EvidenceKind.ADVERSARIAL_VERIFIED,
                EvidenceKind.STATIC_VERIFIED,
                EvidenceKind.RUNTIME_PROBE,
                EvidenceKind.DIFFERENTIAL,
            },
        )

    def test_no_ledger_means_no_side_effects(self):
        assessment = assess(self.hook, self.proposals, DECODE, FakeValidator())
        self.assertTrue(assessment.claims)
        self.assertEqual(EvidenceLedger().claims, ())


class RefutationTests(unittest.TestCase):
    def test_a_refutation_needs_a_verifier_id(self):
        for bad in ("", "  "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    Refutation("h", bad, False, "found nothing")
                self.assertIn("needs a verifier id", str(caught.exception))

    def test_a_refutation_needs_a_finding_even_when_nothing_was_found(self):
        """"Looked and found nothing" and "did not look" must not read the same.

        An empty finding is indistinguishable from a verifier that never ran, and
        `refuted=False` with no finding would clear the adversarial gate on the
        strength of silence.
        """
        for bad in ("", "   ", "\n"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    Refutation("h", "verifier-x", False, bad)
                message = str(caught.exception)
                self.assertIn("needs a finding", message)
                self.assertIn("did not look", message)

    def test_checked_defaults_to_empty(self):
        self.assertEqual(Refutation("h", "v", True, "wrong site").checked, ())


class LoadProposalsTests(unittest.TestCase):
    def write(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "proposals.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_a_flat_list(self):
        path = self.write(
            [
                make_proposal(proposer="agent-a").to_dict(),
                make_proposal(proposer="agent-b", hook_id="install_settings_long_click").to_dict(),
            ]
        )
        grouped = load_proposals(path)
        self.assertEqual(
            set(grouped),
            {"install_settings_long_click_actionbar", "install_settings_long_click"},
        )
        self.assertEqual(grouped["install_settings_long_click_actionbar"][0].proposer, "agent-a")

    def test_reads_a_mapping_and_fills_in_the_hook_id_from_the_key(self):
        # An agent writing per-hook blocks should not have to repeat the id inside
        # each entry; the key is the authority when the entry is silent.
        entry = make_proposal().to_dict()
        del entry["hook_id"]
        path = self.write({"install_settings_long_click_actionbar": [entry]})
        grouped = load_proposals(path)
        self.assertEqual(list(grouped), ["install_settings_long_click_actionbar"])
        self.assertEqual(
            grouped["install_settings_long_click_actionbar"][0].hook_id,
            "install_settings_long_click_actionbar",
        )

    def test_a_mapping_key_groups_several_proposers_under_one_hook(self):
        entries = []
        for name in ("agent-a", "agent-b"):
            entry = make_proposal(proposer=name).to_dict()
            del entry["hook_id"]
            entries.append(entry)
        grouped = load_proposals(self.write({"install_settings_long_click_actionbar": entries}))
        self.assertEqual(
            [item.proposer for item in grouped["install_settings_long_click_actionbar"]],
            ["agent-a", "agent-b"],
        )

    def test_accepts_a_string_path(self):
        path = self.write([make_proposal().to_dict()])
        self.assertEqual(len(load_proposals(str(path))), 1)

    def test_an_entry_contradicting_the_mapping_key_is_refused(self):
        """Regression: the entry used to win silently, moving the answer.

        A proposal filed under one hook's key while naming another was regrouped
        under the name it gave. The cost: an answer arrives on a hook nobody
        proposed for, carrying that hook's marker and counts into `as_operation`,
        while the hook it was actually about is reported as having no proposals.
        Both halves of that read as normal output. A mislabelled entry has to be
        corrected by whoever wrote it, not reassigned by the loader.
        """
        entry = make_proposal(hook_id="install_settings_long_click").to_dict()
        with self.assertRaises(ProposalError) as caught:
            load_proposals(self.write({"install_settings_long_click_actionbar": [entry]}))
        message = str(caught.exception)
        self.assertIn("under 'install_settings_long_click_actionbar'", message)
        self.assertIn("declares hook_id 'install_settings_long_click'", message)
        self.assertIn("corrected, not reassigned", message)

    def test_an_entry_agreeing_with_the_mapping_key_is_fine(self):
        # The guard must not fire on the redundant-but-correct form, which is what
        # `to_dict()` output looks like when it is filed under its own hook.
        entry = make_proposal().to_dict()
        grouped = load_proposals(
            self.write({"install_settings_long_click_actionbar": [entry]})
        )
        self.assertEqual(list(grouped), ["install_settings_long_click_actionbar"])

    def test_propagates_proposal_validation(self):
        entry = make_proposal().to_dict()
        entry["anchor"] = ["    " + ANCHOR[0]]
        with self.assertRaises(ProposalError):
            load_proposals(self.write([entry]))


class AcceptedHostsTests(unittest.TestCase):
    def setUp(self):
        self.hook = make_hook()

    def resolved(self) -> Assessment:
        return assess(
            self.hook,
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
        )

    def unresolved(self) -> Assessment:
        return assess(self.hook, [], DECODE, FakeValidator())

    def test_emits_the_shape_resolve_manifest_expects(self):
        # `resolve_manifest(proposals=...)` maps hook_id to a sequence of candidate
        # descriptors, so the value has to be a list even for a single winner.
        hosts = accepted_hosts([self.resolved()])
        self.assertEqual(hosts, {"install_settings_long_click_actionbar": ["LX/06X7;"]})
        self.assertIsInstance(hosts["install_settings_long_click_actionbar"], list)

    def test_an_unaccepted_assessment_contributes_nothing(self):
        self.assertEqual(accepted_hosts([self.unresolved()]), {})

    def test_mixed_assessments_keep_only_the_accepted_ones(self):
        self.assertEqual(
            list(accepted_hosts([self.unresolved(), self.resolved()])),
            ["install_settings_long_click_actionbar"],
        )

    def test_no_assessments_gives_no_hosts(self):
        self.assertEqual(accepted_hosts([]), {})


class OperationsForTests(unittest.TestCase):
    def setUp(self):
        self.hook = make_hook()
        self.hooks = {self.hook.hook_id: self.hook}

    def resolved(self) -> Assessment:
        return assess(
            self.hook,
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
        )

    def test_an_unaccepted_assessment_raises_and_carries_the_reason(self):
        # Silently skipping it would ship a build missing a hook nobody noticed.
        assessment = assess(self.hook, [], DECODE, FakeValidator())
        with self.assertRaises(ManifestError) as caught:
            operations_for([assessment], self.hooks)
        message = str(caught.exception)
        self.assertIn("was not accepted", message)
        self.assertIn(assessment.reason, message)

    def test_one_unaccepted_assessment_poisons_the_whole_batch(self):
        with self.assertRaises(ManifestError):
            operations_for(
                [self.resolved(), assess(self.hook, [], DECODE, FakeValidator())],
                self.hooks,
            )

    def test_emits_exactly_the_keys_the_applier_reads(self):
        operation, = operations_for([self.resolved()], self.hooks)
        self.assertEqual(
            set(operation),
            {
                "id",
                "descriptor",
                "mode",
                "anchor",
                "expected_anchor_count",
                "marker",
                "expected_marker_count",
                "payload",
            },
        )

    def test_hook_level_fields_come_from_the_hook_not_the_proposal(self):
        """The proposer supplies the site; the manifest supplies the contract.

        Marker, mode and the two counts are what the applier uses to refuse a
        partial or duplicated patch. Taking them from the proposal would let a
        proposer relax its own idempotence check.
        """
        operation, = operations_for([self.resolved()], self.hooks)
        self.assertEqual(operation["id"], self.hook.hook_id)
        self.assertEqual(operation["mode"], "insert_after")
        self.assertEqual(operation["marker"], "Lcom/dfinstagram/SettingsWrapper;")
        self.assertEqual(operation["expected_marker_count"], 2)
        self.assertEqual(operation["expected_anchor_count"], 1)
        self.assertEqual(operation["descriptor"], "LX/06X7;")
        self.assertEqual(operation["anchor"], list(ANCHOR))
        self.assertEqual(operation["payload"], list(PAYLOAD))

    def test_a_replace_mode_hook_carries_its_own_mode_through(self):
        hook = make_hook(
            mode="replace",
            payload=(
                "    # dfinsta_marker",
                "    new-instance <long>, Lcom/dfinstagram/SettingsWrapper;",
            ),
            marker="# dfinsta_marker",
            expected_marker_count=1,
        )
        assessment = assess(
            hook,
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
        )
        operation, = operations_for([assessment], {hook.hook_id: hook})
        self.assertEqual(operation["mode"], "replace")
        self.assertEqual(operation["expected_marker_count"], 1)

    def test_emits_plain_json_serialisable_lists(self):
        operation, = operations_for([self.resolved()], self.hooks)
        self.assertIsInstance(operation["anchor"], list)
        self.assertIsInstance(operation["payload"], list)
        json.dumps(operation)

    def test_no_assessments_gives_no_operations(self):
        self.assertEqual(operations_for([], self.hooks), [])


class RealDecodeIntegrationTests(unittest.TestCase):
    """One end-to-end pass with the real validator over the real 430 decode."""

    def setUp(self):
        if not STOCK_430.is_dir():
            self.skipTest(f"{STOCK_430} is absent (work/ is gitignored)")
        sys.path.insert(0, str(RESOLVER_TOOLS))
        try:
            from validate_candidates import validate
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("tools/resolver is not importable")
        finally:
            sys.path.remove(str(RESOLVER_TOOLS))
        self.validate = validate
        self.hook = {
            hook.hook_id: hook for hook in load_manifest(MANIFEST)
        }["install_settings_long_click_actionbar"]

    def known_good(self) -> Proposal:
        """Derive the answer mechanically, so the fixture cannot itself be wrong."""
        source = (STOCK_430 / "smali_classes6" / "X" / "06X7.smali").read_text(
            encoding="utf-8"
        )
        resolution = resolve_in_source(self.hook, "LX/06X7;", source)
        self.assertTrue(resolution.resolved, resolution.reason)
        return Proposal(
            hook_id=self.hook.hook_id,
            proposer="agent-a",
            descriptor="LX/06X7;",
            anchor=tuple(resolution.anchor),
            payload=tuple(resolution.payload),
            rationale="the five-line anchor separates Options from the follow button",
            evidence=("smali_classes6/X/06X7.smali",),
        )

    def test_the_real_validator_accepts_a_real_site_two_proposers_agree_on(self):
        good = self.known_good()
        second = replace(
            good, proposer="agent-b", rationale="reached it from the AP1 caller graph"
        )
        wrong = replace(
            good,
            proposer="agent-c",
            descriptor="LX/ZZZZ;",
            rationale="confident, fluent, and pointing at a class that does not exist",
        )
        self.assertEqual(good.fingerprint, second.fingerprint)
        self.assertNotEqual(good.fingerprint, wrong.fingerprint)

        assessment = assess(self.hook, [good, second, wrong], STOCK_430, self.validate)
        self.assertTrue(assessment.resolved, assessment.reason)
        self.assertEqual(assessment.accepted, good)
        self.assertIn("2 independent proposers agreed on LX/06X7;", assessment.reason)

        self.assertEqual(assessment.validations[good.key]["verdict"], "OK")
        self.assertEqual(assessment.validations[second.key]["verdict"], "OK")
        self.assertEqual(assessment.validations[wrong.key]["verdict"], "BROKEN")
        self.assertFalse(assessment.validations[wrong.key]["descriptor_resolves"])
        self.assertEqual(assessment.validations[wrong.key]["proposer"], "agent-c")

        # The real row must produce the same claim mapping the fakes assume.
        anchor_claims = {
            claim.detail["proposer"]: claim
            for claim in claims_of(assessment, EvidenceKind.ANCHOR_UNIQUE)
        }
        self.assertIs(anchor_claims["agent-a"].verdict, Verdict.PASSED)
        self.assertIn("smali_classes6/X/06X7.smali", anchor_claims["agent-a"].summary)
        self.assertIs(anchor_claims["agent-c"].verdict, Verdict.FAILED)
        self.assertIn("descriptor_resolves", anchor_claims["agent-c"].summary)

        register_claims = {
            claim.detail["proposer"]: claim
            for claim in claims_of(assessment, EvidenceKind.REGISTERS_SAFE)
        }
        self.assertIs(register_claims["agent-a"].verdict, Verdict.PASSED)
        # The unmatched proposal's liveness was never evaluated, so it is
        # inconclusive rather than being folded into its BROKEN verdict.
        self.assertIs(register_claims["agent-c"].verdict, Verdict.INCONCLUSIVE)


class MutationTests(unittest.TestCase):
    """Delete a guard, show what ships. Each docstring names the production failure."""

    def test_without_the_whitespace_check_a_dirty_anchor_matches_nothing(self):
        """Removing it: the applier receives an anchor that can never match.

        `find_anchors` compares against `line.strip()`, so a leading-space anchor
        finds 0 of 1 and the applier raises "Anchor mismatch" from inside the build,
        naming a file and a count but not the cause. Three 439 Reels anchors were
        submitted exactly this way. The guard turns a build-stage mystery into a
        proposal-stage sentence.
        """
        sys.path.insert(0, str(RECONSTRUCTION_TOOLS))
        try:
            from apply_anchored_patches import find_anchors
        except ImportError:  # pragma: no cover - tool tree is present in-repo
            self.skipTest("reconstruction tools not importable")
        finally:
            sys.path.remove(str(RECONSTRUCTION_TOOLS))

        source = [
            ".class public LX/06X7;",
            ".method public AP1()V",
            "    .locals 15",
            "",
            "    .line 412",
            "    " + ANCHOR[0],
            "",
            "    return-void",
            ".end method",
        ]
        clean = list(ANCHOR)
        dirty = ["    " + ANCHOR[0]]
        self.assertEqual(len(find_anchors(source, clean)), 1)
        self.assertEqual(find_anchors(source, dirty), [])

        # The guard is what stops the dirty form ever becoming an operation.
        with self.assertRaises(ProposalError):
            make_proposal(anchor=tuple(dirty))
        # Bypassing __post_init__ the way a deleted check would, the operation is
        # built happily and carries the unmatchable anchor straight to the applier.
        smuggled = Proposal.__new__(Proposal)
        object.__setattr__(smuggled, "hook_id", "install_settings_long_click_actionbar")
        object.__setattr__(smuggled, "proposer", "agent-a")
        object.__setattr__(smuggled, "descriptor", "LX/06X7;")
        object.__setattr__(smuggled, "anchor", tuple(dirty))
        object.__setattr__(smuggled, "payload", PAYLOAD)
        object.__setattr__(smuggled, "rationale", "")
        object.__setattr__(smuggled, "evidence", ())
        operation = smuggled.as_operation(make_hook())
        self.assertEqual(operation["anchor"], dirty)
        self.assertEqual(find_anchors(source, operation["anchor"]), [])
        self.assertEqual(operation["expected_anchor_count"], 1)

    def test_folding_rationale_into_the_fingerprint_stops_agreement_working(self):
        """Adding it: two proposers who found the same site no longer count as agreeing.

        Prose agreement between language models is worthless as corroboration, but
        prose *disagreement* is the normal case even when the answer is identical.
        Include it in the identity and every proposal becomes its own group, the
        majority is always 1, and the hard hooks escalate forever — the check would
        look like it was working while never being able to pass.
        """
        proposals = [
            make_proposal(proposer="agent-a", rationale="the label id is the Options string"),
            make_proposal(proposer="agent-b", rationale="reached it from the AP1 caller graph"),
        ]
        self.assertEqual(proposals[0].fingerprint, proposals[1].fingerprint)
        baseline = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertTrue(baseline.resolved)

        mutant = property(
            lambda self: canonical_sha256(
                {
                    "descriptor": self.descriptor,
                    "anchor": list(self.anchor),
                    "payload": list(self.payload),
                    # The mutation, and the only difference from the real property.
                    "rationale": self.rationale,
                }
            )
        )
        with mock.patch.object(Proposal, "fingerprint", mutant):
            self.assertNotEqual(proposals[0].fingerprint, proposals[1].fingerprint)
            self.assertEqual(len(group_by_fingerprint(proposals)), 2)
            broken = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertFalse(broken.resolved)
        self.assertIn("1 of 2 distinct proposers", broken.reason)

    def test_without_the_dedup_one_agent_manufactures_its_own_consensus(self):
        """Removing it: re-running one agent three times clears the agreement gate.

        This is the cheapest possible attack and needs no malice — a retry loop
        does it by accident. The holdout's dangerous proposer was fluent, confident
        and wrong; three copies of it would be accepted here as three independent
        corroborations of a fabricated register claim, and the ledger would keep a
        `proposer_agreement` claim reading "3 of 3" to say so.
        """
        proposals = [make_proposal(proposer="agent-a") for _ in range(3)]
        baseline = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertFalse(baseline.resolved)
        self.assertIn("only one proposer answered", baseline.reason)
        self.assertIsNot(
            only_claim(baseline, EvidenceKind.PROPOSER_AGREEMENT).verdict, Verdict.PASSED
        )

        # `one_per_proposer` is the single place the collapse happens, for both the
        # decision and the claim. Neutralise it and both go wrong together.
        with mock.patch(
            "dfinsta_pipeline.proposals.one_per_proposer", side_effect=list
        ):
            forged = assess(make_hook(), proposals, DECODE, FakeValidator())
        self.assertTrue(forged.resolved)
        self.assertEqual(forged.accepted, proposals[0])
        self.assertIn("3 independent proposers agreed", forged.reason)
        claim = only_claim(forged, EvidenceKind.PROPOSER_AGREEMENT)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("3 of 3 proposers agreed", claim.summary)

    def test_keying_validations_by_proposer_accepts_a_proposal_the_validator_refused(self):
        """Reverting `Proposal.key` to the proposer id: a refused answer ships.

        One proposer with two answers keeps one validator row under a proposer
        key, and whichever answer is looked up inherits it. Below, agent-a's
        `LX/BAD;` is refused by the validator and its second answer passes; under
        the mutation `LX/BAD;` is accepted, the reason says it "passed the
        validator", and the `anchor_unique` claim filed for it quotes the *other*
        proposal's smali path. That is a failed mechanical check reaching the
        ledger as deterministic evidence for a site nothing ever checked — the
        module's own stated failure mode arriving through a bookkeeping alias.
        """
        validator = FakeValidator(
            {
                "LX/BAD;": broken_row(
                    "descriptor not found in this decode", descriptor_resolves=False
                ),
                "LX/06X7;": ok_row(),
            }
        )
        refused = make_proposal(proposer="agent-a", descriptor="LX/BAD;")
        proposals = [
            refused,
            make_proposal(proposer="agent-a", descriptor="LX/06X7;"),
            make_proposal(proposer="agent-b", descriptor="LX/BAD;"),
        ]
        baseline = assess(make_hook(), proposals, DECODE, validator)
        self.assertFalse(baseline.resolved)

        with mock.patch.object(Proposal, "key", property(lambda self: self.proposer)):
            forged = assess(make_hook(), proposals, DECODE, validator)
        self.assertTrue(forged.resolved)
        self.assertEqual(forged.accepted, refused)
        self.assertIn("passed the validator", forged.reason)
        self.assertEqual(set(forged.validations), {"agent-a", "agent-b"})
        agent_a_claims = [
            claim
            for claim in claims_of(forged, EvidenceKind.ANCHOR_UNIQUE)
            if claim.detail["proposer"] == "agent-a"
        ]
        self.assertEqual(len(agent_a_claims), 2)
        for claim in agent_a_claims:
            self.assertIs(claim.verdict, Verdict.PASSED)
            self.assertIn("smali_classes6/X/06X7.smali", claim.summary)

    def test_mapping_an_unevaluated_liveness_check_to_passed_would_ship_it_as_proven(self):
        """Mapping `registers_safe is None` to PASSED: an unrun check counts as evidence.

        `None` is what the validator writes when the anchor never matched and
        liveness was therefore never examined. Recorded as `passed`, the ledger's
        `registers_safe` item is satisfied and the hook advances on a check that
        did not happen — the same substitution of assertion for evidence that let a
        holdout proposer claim a live listener register was "usually null".
        """
        row = ok_row(registers_safe=None, registers_note="not evaluated: anchor did not match")
        assessment = assess(
            make_hook(), [make_proposal()], DECODE, FakeValidator(default=row)
        )
        real = only_claim(assessment, EvidenceKind.REGISTERS_SAFE)
        self.assertIs(real.verdict, Verdict.INCONCLUSIVE)

        subject = Subject(
            "install_settings_long_click_actionbar", "agent", proposed_by="agent-a"
        )
        honest = EvidenceLedger()
        honest.register(subject)
        honest.record(real)
        status = {
            item.kind: item
            for item in honest.readiness(subject.hook_id).statuses
        }[EvidenceKind.REGISTERS_SAFE]
        self.assertIs(status.verdict, Verdict.INCONCLUSIVE)
        self.assertFalse(status.satisfied)

        mutated = EvidenceLedger()
        mutated.register(subject)
        mutated.record(replace(real, verdict=Verdict.PASSED))
        forged = {
            item.kind: item
            for item in mutated.readiness(subject.hook_id).statuses
        }[EvidenceKind.REGISTERS_SAFE]
        self.assertTrue(forged.satisfied)
        # Same never-evaluated note, now standing as proof.
        self.assertIn("not evaluated", str(forged.claim.detail["row"]["registers_note"]))


class ReportedDefectTests(unittest.TestCase):
    """Regressions for defects this suite characterised and the module then fixed.

    Each docstring records what the defect would have cost in production, because
    that is the reason to keep the test rather than the reason it once failed.

    Three more of these live with the class they belong to, because that is where
    a reader will look for them:

        payload absent from the fingerprint    FingerprintTests
        an empty anchor line slipped through   ProposalValidationTests
        entry hook_id beat the map key         LoadProposalsTests
    """

    def test_a_proposers_second_answer_no_longer_inherits_the_first_ones_verdict(self):
        """The most serious one: a refused proposal could be accepted and reported clean.

        `validate_proposals` stored rows keyed by `proposer` and `assess` looked
        `surviving` up the same way, so one proposer submitting two different
        answers kept only the LAST row and *both* of its proposals inherited it.

        Below, agent-a proposes a descriptor the validator refuses and then one it
        accepts. The refused answer is the one two proposers agree on. Under the
        defect it was accepted, with a reason claiming "it passed the validator",
        while the row that actually said BROKEN had been discarded — and its
        `anchor_unique` claim was filed as `passed` quoting the *other* proposal's
        smali path, so the ledger held deterministic evidence for a site nothing
        ever checked. A build would have shipped a patch against `LX/BAD;` and the
        report would have said three checks agreed about it.

        Keyed by `Proposal.key`, each answer keeps its own verdict, the agreed
        answer is seen to have failed, and the hook escalates.
        """
        hook = make_hook()
        validator = FakeValidator(
            {
                "LX/BAD;": broken_row(
                    "descriptor not found in this decode", descriptor_resolves=False
                ),
                "LX/06X7;": ok_row(),
            }
        )
        refused = make_proposal(proposer="agent-a", descriptor="LX/BAD;")
        accepted_by_validator = make_proposal(proposer="agent-a", descriptor="LX/06X7;")
        seconded = make_proposal(proposer="agent-b", descriptor="LX/BAD;")
        assessment = assess(
            hook, [refused, accepted_by_validator, seconded], DECODE, validator
        )
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("failed the deterministic validator", assessment.reason)
        self.assertIn("Agreement is not evidence", assessment.reason)

        # Three proposals, three rows, each carrying its own verdict.
        self.assertEqual(len(assessment.validations), 3)
        self.assertEqual(assessment.validations[refused.key]["verdict"], "BROKEN")
        self.assertEqual(
            assessment.validations[accepted_by_validator.key]["verdict"], "OK"
        )
        self.assertEqual(assessment.validations[seconded.key]["verdict"], "BROKEN")

        # And agent-a's two claims say different things, because its two answers
        # checked out differently.
        agent_a_claims = [
            claim
            for claim in claims_of(assessment, EvidenceKind.ANCHOR_UNIQUE)
            if claim.detail["proposer"] == "agent-a"
        ]
        self.assertEqual(
            [claim.verdict for claim in agent_a_claims], [Verdict.FAILED, Verdict.PASSED]
        )
        self.assertIn("descriptor_resolves", agent_a_claims[0].summary)
        self.assertIn("smali_classes6/X/06X7.smali", agent_a_claims[1].summary)

    def test_the_agreement_claim_counts_distinct_proposers_not_submissions(self):
        """The claim used to be built from every proposal, so repetition read as consensus.

        `assess` deduplicated before deciding but handed `agreement_claim` the raw
        list, so one agent run three times filed `proposer_agreement` as `passed`
        reading "3 of 3 proposers agreed" with `detail["agreed"] == 3`. The
        decision was still correctly refused — which is what made it dangerous:
        the ledger is the artifact a human reads and the thing `readiness()`
        consults, and it recorded the manufactured consensus as satisfied. Re-run
        the agent once more under a second id and the hook advances carrying a
        claim that was already false when it was written.
        """
        trio = [make_proposal(proposer="agent-a") for _ in range(3)]
        assessment = assess(make_hook(), trio, DECODE, FakeValidator())
        self.assertFalse(assessment.resolved)

        claim = only_claim(assessment, EvidenceKind.PROPOSER_AGREEMENT)
        self.assertIsNot(claim.verdict, Verdict.PASSED)
        self.assertFalse(claim.verdict.satisfies)
        self.assertIn("1 of 1 proposers agreed", claim.summary)
        self.assertEqual(claim.detail["proposals"], 1)
        self.assertEqual(claim.detail["answered"], 1)
        self.assertEqual(claim.detail["agreed"], 1)

        ledger = EvidenceLedger()
        assess(make_hook(), trio, DECODE, FakeValidator(), ledger=ledger)
        status = {
            item.kind: item
            for item in ledger.readiness("install_settings_long_click_actionbar").statuses
        }[EvidenceKind.PROPOSER_AGREEMENT]
        self.assertFalse(status.satisfied)

    def test_the_claim_still_counts_genuinely_distinct_proposers(self):
        # The dedup must not swallow real corroboration, or the fix would make the
        # agreement item unsatisfiable and every agent hook would escalate forever.
        pair = [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")]
        claim = only_claim(
            assess(make_hook(), pair, DECODE, FakeValidator()),
            EvidenceKind.PROPOSER_AGREEMENT,
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertIn("2 of 2 proposers agreed", claim.summary)
        self.assertEqual(claim.detail["agreed"], 2)

    def test_a_hook_with_no_proposals_is_registered_and_reported(self):
        """The empty case used to return before the ledger, so nothing escalated.

        `assess` short-circuited ahead of `ledger.register`, and the evidence
        module's stated intent is the opposite: "a hook with no claims at all
        escalates with every required kind recorded as `not_exercised`". With
        nothing registered, `readiness()` raised `EvidenceError` instead of
        reporting and the hook was absent from `report()["hooks"]` — so a hook
        nobody even attempted was indistinguishable, in the only artifact a gate
        reads, from a hook that does not exist. Silence reads as "nothing to worry
        about"; the whole point of the ledger is that absence is never a pass.
        """
        ledger = EvidenceLedger()
        assessment = assess(make_hook(), [], DECODE, FakeValidator(), ledger=ledger)
        self.assertFalse(assessment.resolved)
        self.assertIn("no proposals were produced", assessment.reason)
        # Registered, but with no claims: there is nothing to attest to.
        self.assertEqual(assessment.claims, ())
        self.assertEqual(ledger.claims, ())

        readiness = ledger.readiness("install_settings_long_click_actionbar")
        self.assertFalse(readiness.ready)
        # `agent` provenance, so all seven kinds are demanded and all seven are
        # visibly absent rather than quietly missing.
        self.assertEqual(len(readiness.statuses), len(EvidenceKind))
        self.assertEqual(set(readiness.missing), set(EvidenceKind))
        for status in readiness.statuses:
            with self.subTest(kind=status.kind.value):
                self.assertIs(status.verdict, Verdict.NOT_EXERCISED)
                self.assertFalse(status.satisfied)

        report = ledger.report()
        self.assertIn("install_settings_long_click_actionbar", report["hooks"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["escalations"]), 1)

    def test_a_single_proposer_is_told_apart_from_genuine_ambiguity(self):
        """One answer used to reach the gate worded exactly like three conflicting ones.

        The share branch covered both, so a hook only one agent even attempted was
        escalated as "genuine ambiguity ... rather than being broken by ranking".
        There is no ambiguity to break — there is one voice and no corroboration —
        and the two cases want opposite responses: find another proposer, versus
        have a human read the disagreement. Reading the wrong one wastes the gate's
        time on a comparison that does not exist.
        """
        lonely = assess(make_hook(), [make_proposal()], DECODE, FakeValidator())
        self.assertFalse(lonely.resolved)
        self.assertIn("only one proposer answered", lonely.reason)
        self.assertIn("nothing to corroborate it", lonely.reason)
        self.assertIn("a single answer cannot supply it", lonely.reason)
        self.assertNotIn("ambiguity", lonely.reason)

        # Repetition is the same case: three submissions, still one voice.
        repeated = assess(
            make_hook(),
            [make_proposal(proposer="agent-a") for _ in range(3)],
            DECODE,
            FakeValidator(),
        )
        self.assertIn("only one proposer answered", repeated.reason)
        self.assertNotIn("ambiguity", repeated.reason)

        # Two or more proposers who disagree keep the ambiguity wording, which is
        # the case it was always meant for.
        ambiguous = assess(
            make_hook(),
            [
                make_proposal(proposer="agent-a", descriptor="LX/06X7;"),
                make_proposal(proposer="agent-b", descriptor="LX/0Di2;"),
            ],
            DECODE,
            FakeValidator(),
        )
        self.assertIn("1 of 2 distinct proposers", ambiguous.reason)
        self.assertIn("genuine ambiguity", ambiguous.reason)
        self.assertNotIn("only one proposer answered", ambiguous.reason)

    def test_a_self_refuting_verifier_escalates_instead_of_aborting_the_run(self):
        """An `EvidenceError` used to escape `assess` mid-assessment.

        Everywhere else `assess` turns a failure into an escalation — a validator
        that raises becomes a BROKEN row. A refutation whose verifier id matched
        the winning proposer instead built a claim the ledger refuses, and the
        `EvidenceError` came out of `assess` *after* the subject was registered and
        four claims were already on disk. The caller got no `Assessment` at all, so
        the run died on the one input a human most needs described; and a retry
        re-recorded the surviving claims, inflating `attempts` and tripping the
        ledger's own retry-to-green flag on evidence that never failed.

        Now the refutation is separated out before any claim is built: nothing the
        proposer said is recorded, and the escalation says why.
        """
        ledger = EvidenceLedger()
        assessment = assess(
            make_hook(),
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    "install_settings_long_click_actionbar",
                    "agent-a",
                    False,
                    "looks fine to me",
                )
            ],
            ledger=ledger,
        )
        self.assertFalse(assessment.resolved)
        self.assertIsNone(assessment.accepted)
        self.assertIn("agent-a", assessment.reason)
        self.assertIn("helped produce the accepted answer and also", assessment.reason)
        self.assertIn("not independent evidence", assessment.reason)
        self.assertIn("genuine second opinion", assessment.reason)

        # No adversarial claim exists at all — the finding is not evidence, so it
        # is not filed as any verdict, not even a failing one.
        self.assertEqual(claims_of(assessment, EvidenceKind.ADVERSARIAL_VERIFIED), [])
        self.assertEqual(
            ledger.claims_for(
                "install_settings_long_click_actionbar",
                EvidenceKind.ADVERSARIAL_VERIFIED,
            ),
            (),
        )
        self.assertNotIn("agent-a", {claim.actor for claim in ledger.claims})

        # The rest of the run still happened, so the gate sees what was checked.
        self.assertEqual(
            [item.proposer for item in assessment.proposals], ["agent-a", "agent-b"]
        )
        self.assertEqual(len(assessment.validations), 2)
        self.assertEqual(len(claims_of(assessment, EvidenceKind.ANCHOR_UNIQUE)), 2)
        self.assertFalse(ledger.readiness("install_settings_long_click_actionbar").ready)

        # Re-running is now idempotent in the way that matters: it does not raise,
        # and the adversarial item is still simply never exercised.
        again = assess(
            make_hook(),
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    "install_settings_long_click_actionbar",
                    "agent-a",
                    False,
                    "looks fine to me",
                )
            ],
            ledger=ledger,
        )
        self.assertFalse(again.resolved)
        status = {
            item.kind: item
            for item in ledger.readiness("install_settings_long_click_actionbar").statuses
        }[EvidenceKind.ADVERSARIAL_VERIFIED]
        self.assertIs(status.verdict, Verdict.NOT_EXERCISED)
        self.assertFalse(status.recovered_from_failure)

    def test_a_self_refutation_is_reported_even_when_it_actually_refutes(self):
        # `refuted=True` from the proposer is not a refusal either: an agent
        # cannot be trusted to condemn its own answer any more than to clear it,
        # and the escalation must name the independence problem, not the finding.
        assessment = assess(
            make_hook(),
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    "install_settings_long_click_actionbar",
                    "agent-a",
                    True,
                    "on reflection v13 is live here",
                )
            ],
        )
        self.assertFalse(assessment.resolved)
        self.assertIn("not independent evidence", assessment.reason)
        self.assertNotIn("a verifier refuted the proposal", assessment.reason)


class IndependenceTests(unittest.TestCase):
    """A verifier that helped write the answer is not independent evidence.

    Both of these began as reported gaps and were fixed. They are kept because
    each is a distinct way for a proposer to end up clearing its own work, and
    neither is visible to the ledger: `Subject.proposed_by` records one name, so
    the ledger cannot notice a co-author or a whitespace variant of one.
    """

    def test_a_verifier_id_differing_only_in_whitespace_is_still_the_proposer(self):
        """`assess` used to compare with `==` while the ledger compares stripped.

        `evidence.py` says exactly why it normalises — "exact equality would let
        one trailing space defeat the check" — and the separation in `assess` did
        not. With a ledger the resulting `EvidenceError` escaped `assess` after
        the subject and four claims were already filed; with no ledger nothing
        refused it at all and the proposal was accepted carrying an
        `adversarial_verified` claim produced by its own proposer.
        """
        proposals = [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")]
        refutations = [
            Refutation(
                "install_settings_long_click_actionbar",
                "agent-a ",
                False,
                "looks fine to me",
            )
        ]
        for ledger in (EvidenceLedger(), None):
            with self.subTest(ledger=ledger is not None):
                assessment = assess(
                    make_hook(), proposals, DECODE, FakeValidator(), refutations, ledger=ledger
                )
                self.assertFalse(assessment.resolved)
                self.assertIn("not independent evidence", assessment.reason)
                self.assertEqual(
                    claims_of(assessment, EvidenceKind.ADVERSARIAL_VERIFIED), []
                )

    def test_a_co_proposer_of_the_accepted_answer_may_not_verify_it(self):
        """Only `winner_group[0].proposer` used to be disqualified.

        Two proposers reaching the same answer is what acceptance is built on, so
        both have attested to it. Treating only the first as the proposer let the
        *other* author supply the adversarial evidence for the answer it co-wrote,
        and `Subject.proposed_by` records only one name, so the ledger had no way
        to notice.
        """
        proposals = [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")]
        self.assertEqual(proposals[0].fingerprint, proposals[1].fingerprint)
        ledger = EvidenceLedger()
        assessment = assess(
            make_hook(),
            proposals,
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    "install_settings_long_click_actionbar",
                    "agent-b",
                    False,
                    "I checked my own answer again and it still looks right",
                )
            ],
            ledger=ledger,
        )
        self.assertFalse(assessment.resolved)
        self.assertIn("agent-b", assessment.reason)
        self.assertIn("not independent evidence", assessment.reason)
        self.assertEqual(claims_of(assessment, EvidenceKind.ADVERSARIAL_VERIFIED), [])
        self.assertEqual(
            ledger.claims_for(
                "install_settings_long_click_actionbar",
                EvidenceKind.ADVERSARIAL_VERIFIED,
            ),
            (),
        )

    def test_a_genuine_third_party_verifier_is_still_accepted(self):
        """The guard must not over-fire: a real outside reviewer is the whole point."""
        ledger = EvidenceLedger()
        assessment = assess(
            make_hook(),
            [make_proposal(proposer="agent-a"), make_proposal(proposer="agent-b")],
            DECODE,
            FakeValidator(),
            refutations=[
                Refutation(
                    "install_settings_long_click_actionbar",
                    "verifier-1",
                    False,
                    "tried to break it and could not",
                )
            ],
            ledger=ledger,
        )
        self.assertTrue(assessment.resolved)
        claim = only_claim(assessment, EvidenceKind.ADVERSARIAL_VERIFIED)
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertEqual(claim.actor, "verifier-1")


if __name__ == "__main__":
    unittest.main()

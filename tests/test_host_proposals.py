"""Tests for the narrow question: WHICH class, asked of k blind proposers.

A new file rather than more of `test_proposals.py`, for two reasons. That suite
is about `proposals.py`'s gate — what may be believed once an answer exists — and
runs to two thousand lines already. This is about `proposer.py`, which had no
tests at all, and about a different question: whether the thing we *ask* an agent
still measures the agent rather than our own prompt.

The first full k-proposer run against Instagram 439 is what these are written
against. Three blind proposers, answers physically absent, and the result was 2
of 3 on the host and 1 of 3 by effect — because the two who were right wrote a
2-line anchor with a 16-line payload and a 4-line anchor with a 2-line payload.
`assess` refused, correctly, having been asked to compare patches. The manifest
already owns the patch; only the class varies. So `host_prompt` asks for the
class and nothing else.

The test this file exists for is
`BlindHoldoutTests.test_the_host_prompt_never_shows_a_proposer_hook_constraints`.
`hook.constraints` records what LAST version's patch looked like — the register
the previous port used, the type it compared against, the anchor length that
disambiguated — and any of it in the prompt makes every number this pipeline
produces about agent capability meaningless. Its absence is asserted WITH a
positive control, because a search that cannot succeed always passes.
"""

import json
import re
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from dfinsta_pipeline.evidence import HOST_ONLY, Verdict, agreement_claim
from dfinsta_pipeline.hook_manifest import Hook, HostFingerprint, load_manifest
from dfinsta_pipeline.proposals import (
    CLASS_DESCRIPTOR,
    HostProposal,
    ProposalError,
    host_agreement,
    one_per_proposer,
)
from dfinsta_pipeline.proposer import (
    HOST_SCHEMA,
    PROPOSAL_SCHEMA,
    HostRun,
    collect_hosts,
    host_prompt,
    host_verifier_prompt,
    parse_host,
    parse_proposal,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "hooks.json"
STOCK_430 = ROOT / "work" / "430-clean-build-v2" / "stock-430"
STOCK_439 = ROOT / "work" / "439-build-v1" / "stock-439"
SANDBOX = Path("/sandbox/instagram-439")

#: Hosts this project has actually resolved and published in its own history. The
#: schema's example must not be one of them: an earlier `PROPOSAL_SCHEMA` used the
#: real 439 answer in its example and handed the result to every proposer through
#: the schema itself.
PUBLISHED_ANSWERS = ("LX/0DnT;", "LX/06X7;", "LX/09rb;")

#: Any smali class descriptor, for finding one that leaked into a prompt.
DESCRIPTOR_IN_TEXT = re.compile(r"L[A-Za-z0-9_$/]+;")


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
        "intent_constraints": (
            "must take effect ONLY on the signed-in user's own profile",
        ),
        "constraints": (
            "the same long-click iput appears TWICE in the host; the five-line anchor "
            "including the drawable and label is what disambiguates",
        ),
    }
    fields.update(overrides)
    return Hook(**fields)  # type: ignore[arg-type]


def make_host(**overrides: object) -> HostProposal:
    fields: dict[str, object] = {
        "hook_id": "install_settings_long_click_actionbar",
        "proposer": "agent-a",
        "descriptor": "LX/06X7;",
        "smali_path": "smali_classes6/X/06X7.smali",
    }
    fields.update(overrides)
    return HostProposal(**fields)  # type: ignore[arg-type]


def answer(descriptor: str = "LX/06X7;", **extra: Any) -> str:
    """One agent's reply, as text with JSON in it — the shape a runner returns."""
    body: dict[str, Any] = {
        "descriptor": descriptor,
        "smali_path": "smali_classes6/X/06X7.smali",
        "evidence": ["smali_classes6/X/06X7.smali:412"],
    }
    body.update(extra)
    return f"I looked at three candidates.\n\n```json\n{json.dumps(body)}\n```\n"


class Recorder:
    """An `AgentRunner` that keeps every prompt it was given."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class HostSchemaTests(unittest.TestCase):
    """The schema is half the prompt, so what it does NOT say is load-bearing."""

    def test_it_asks_for_a_host_and_never_for_a_patch(self):
        # The entire point: the manifest owns the anchor and the payload, and
        # asking an agent to reinvent them is what turned a 2-of-3 host agreement
        # into a 1-of-3 effect agreement on the first real run.
        self.assertNotIn("anchor", HOST_SCHEMA["properties"])
        self.assertNotIn("payload", HOST_SCHEMA["properties"])
        self.assertIn("anchor", PROPOSAL_SCHEMA["properties"])
        self.assertIn("payload", PROPOSAL_SCHEMA["properties"])

    def test_it_refuses_extra_fields_the_way_the_proposal_schema_does(self):
        self.assertIs(HOST_SCHEMA["additionalProperties"], False)
        self.assertEqual(HOST_SCHEMA["type"], "object")

    def test_the_required_fields_are_the_claim_and_its_evidence(self):
        self.assertEqual(
            set(HOST_SCHEMA["required"]), {"descriptor", "smali_path", "evidence"}
        )
        # Optional, because an agent that considered nothing else or established
        # everything must not be pushed into inventing entries.
        self.assertEqual(
            set(HOST_SCHEMA["properties"]) - set(HOST_SCHEMA["required"]),
            {"alternatives", "unresolved"},
        )

    def test_no_confidence_score_is_asked_for(self):
        # Nothing downstream reads one, and a proposer asked for one supplies a
        # number that reads as evidence.
        self.assertNotIn("confidence", HOST_SCHEMA["properties"])

    def test_the_example_descriptor_is_not_a_published_answer(self):
        for schema, name in ((HOST_SCHEMA, "HOST"), (PROPOSAL_SCHEMA, "PROPOSAL")):
            example = schema["properties"]["descriptor"]["description"]
            for real in PUBLISHED_ANSWERS:
                with self.subTest(schema=name, answer=real):
                    self.assertNotIn(real, example)
            # There is still an example, or the shape requirement is unstated and
            # the check above passes because no descriptor is shown at all.
            self.assertTrue(DESCRIPTOR_IN_TEXT.search(example))

    def test_the_example_descriptor_names_no_class_in_any_decode(self):
        """"Made up" as a measurement rather than as a claim.

        `PROPOSAL_SCHEMA` carried `LX/0aaa;` under a comment calling it
        "deliberately a made-up descriptor". It is a real class in both the 430
        and the 439 decode — `smali_classes15/X/0aaa.smali` and
        `smali_classes13/X/0aaa.smali`. Not the answer to any hook, so the cost
        was small, but the property the comment claimed was never checked and was
        false. A proposer following the example literally opens a real class.
        """
        decodes = [path for path in (STOCK_430, STOCK_439) if path.is_dir()]
        if not decodes:
            self.skipTest("no decode is present (work/ is gitignored)")
        for schema, name in ((HOST_SCHEMA, "HOST"), (PROPOSAL_SCHEMA, "PROPOSAL")):
            example = DESCRIPTOR_IN_TEXT.search(
                schema["properties"]["descriptor"]["description"]
            )
            assert example is not None
            relative = example.group(0)[1:-1] + ".smali"
            for decode in decodes:
                with self.subTest(schema=name, decode=decode.name):
                    found = sorted(decode.glob(f"smali*/{relative}"))
                    self.assertEqual(
                        found, [], f"the schema's example names a real class: {found}"
                    )
                    # Positive control: the same glob shape finds a class that IS
                    # in this decode, so the assertion above is not passing
                    # because the search could never succeed.
                    self.assertTrue(sorted(decode.glob("smali*/X/0aaa.smali")))


class HostProposalTests(unittest.TestCase):
    """A malformed host answer must be refused where the message can say why."""

    def test_a_minimal_host_proposal_is_accepted(self):
        proposal = make_host()
        self.assertEqual(proposal.descriptor, "LX/06X7;")
        self.assertEqual(proposal.evidence, ())
        self.assertEqual(proposal.alternatives, ())

    def test_a_missing_hook_id_is_rejected(self):
        for bad in ("", "  ", "\n"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ProposalError) as caught:
                    make_host(hook_id=bad)
                self.assertIn("needs a hook_id", str(caught.exception))

    def test_a_missing_proposer_is_rejected_and_says_why_it_matters(self):
        # Without a proposer id, k answers from one agent cannot be told apart
        # from k answers from k agents — the whole independence check.
        with self.assertRaises(ProposalError) as caught:
            make_host(proposer="  ")
        self.assertIn("needs a proposer id", str(caught.exception))
        self.assertIn("independence", str(caught.exception))

    def test_a_missing_descriptor_is_rejected(self):
        with self.assertRaises(ProposalError) as caught:
            make_host(descriptor="   ")
        self.assertIn("needs a host descriptor", str(caught.exception))

    def test_a_descriptor_that_is_not_a_class_descriptor_is_rejected(self):
        """Each of these resolves to no class, and the Resolve stage says so wrongly.

        `search_hosts` looks the string up in the index verbatim and reports a miss
        as "does not exist in this version. Obfuscated names are recycled..." — a
        version-drift diagnosis handed to whoever is on the port, for an answer
        that was never a class descriptor. The method-qualified form is the one to
        expect: an agent that has found the *site* names the site.
        """
        for bad in (
            "LX/06X7;->AP1",  # the site, not the class
            "LX/06X7;->AP1(Landroid/view/View;)V",
            "LX/06X7",  # no terminator
            "X/06X7;",  # no leading L
            "[LX/06X7;",  # an array cannot be a host
            "I",  # a primitive cannot be a host
            "LX/06X7;LX/0Di2;",  # two of them
            "smali_classes6/X/06X7.smali",  # the path, not the descriptor
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ProposalError) as caught:
                    make_host(descriptor=bad)
                message = str(caught.exception)
                self.assertIn("is not a smali class descriptor", message)
                self.assertIn("install_settings_long_click_actionbar", message)

    def test_real_descriptor_shapes_are_accepted(self):
        # The guard must not over-fire: obfuscated segments start with digits, and
        # d8 emits `-$$Lambda$` classes that could legitimately be a host.
        for good in (
            "LX/06X7;",
            "Lcom/instagram/profile/fragment/UserDetailFragment;",
            "Lcom/instagram/Foo$Bar;",
            "Lcom/foo/-$$Lambda$Bar$1;",
        ):
            with self.subTest(good=good):
                self.assertTrue(CLASS_DESCRIPTOR.match(good))
                self.assertEqual(make_host(descriptor=good).descriptor, good)

    def test_from_dict_round_trips_through_to_dict(self):
        proposal = make_host(
            evidence=("smali_classes6/X/06X7.smali:412",),
            alternatives=("LX/0Di2; builds the row but never attaches a listener",),
            unresolved=("could not tell which MobileConfig flag selects this",),
        )
        self.assertEqual(HostProposal.from_dict(proposal.to_dict()), proposal)
        json.dumps(proposal.to_dict())

    def test_from_dict_propagates_validation(self):
        data = make_host().to_dict()
        data["descriptor"] = "LX/06X7;->AP1"
        with self.assertRaises(ProposalError):
            HostProposal.from_dict(data)

    def test_is_frozen(self):
        with self.assertRaises(Exception):
            make_host().descriptor = "LX/0Di2;"  # type: ignore[misc]


class ParseHostTests(unittest.TestCase):
    """Mirrors `parse_proposal`'s discipline, because the failures are the same."""

    def test_it_reads_json_out_of_surrounding_prose(self):
        proposal = parse_host("hook", "agent-a", answer())
        self.assertEqual(proposal.descriptor, "LX/06X7;")
        self.assertEqual(proposal.smali_path, "smali_classes6/X/06X7.smali")
        self.assertEqual(proposal.evidence, ("smali_classes6/X/06X7.smali:412",))
        self.assertEqual(proposal.proposer, "agent-a")

    def test_it_takes_the_last_object_not_the_first(self):
        """An agent that revises itself leaves the draft behind.

        The draft is the answer it decided against. Taking it grades the wrong
        answer and does so silently, because a draft is well-formed and plausible
        — and on the host path the two objects differ by exactly one field, so
        nothing downstream could notice.
        """
        text = (
            "First pass:\n"
            + answer("LX/0Di2;")
            + "\nOn checking the caller graph that class only logs. Revised:\n"
            + answer("LX/06X7;")
        )
        self.assertEqual(parse_host("hook", "agent-a", text).descriptor, "LX/06X7;")

    def test_it_skips_a_trailing_object_that_is_not_an_answer(self):
        # An agent that appends a note object after its answer must not have the
        # note graded; the last *usable* object is the answer.
        text = answer() + '\n{"not": "an answer"'  # unbalanced, so never a candidate
        self.assertEqual(parse_host("hook", "agent-a", text).descriptor, "LX/06X7;")

    def test_an_unknown_field_is_refused(self):
        """One of the three proposers in the first real run was dropped for this.

        An invented field is an agent that answered a question of its own. Taking
        the rest of the object and ignoring the extra would grade an answer that
        is not the one it gave.
        """
        with self.assertRaises(ProposalError) as caught:
            parse_host("hook", "agent-a", answer(confidence=0.9))
        message = str(caught.exception)
        self.assertIn("unknown field(s)", message)
        self.assertIn("confidence", message)
        self.assertIn("agent-a", message)

    def test_anchor_and_payload_are_unknown_fields_here(self):
        # A proposer answering the old question against the new schema is dropped
        # rather than half-read.
        with self.assertRaises(ProposalError) as caught:
            parse_host("hook", "agent-a", answer(anchor=["iput-object v13, v1, LX/09rb;->A0H:I"]))
        self.assertIn("anchor", str(caught.exception))

    def test_a_missing_required_field_is_refused(self):
        for missing in ("descriptor", "smali_path", "evidence"):
            with self.subTest(missing=missing):
                body = {
                    "descriptor": "LX/06X7;",
                    "smali_path": "smali_classes6/X/06X7.smali",
                    "evidence": ["a line"],
                }
                del body[missing]
                with self.assertRaises(ProposalError) as caught:
                    parse_host("hook", "agent-a", json.dumps(body))
                self.assertIn("omitted required field(s)", str(caught.exception))
                self.assertIn(missing, str(caught.exception))

    def test_a_descriptor_that_is_not_one_is_refused(self):
        with self.assertRaises(ProposalError) as caught:
            parse_host("hook", "agent-a", answer("LX/06X7;->AP1"))
        self.assertIn("is not a smali class descriptor", str(caught.exception))

    def test_no_json_at_all_is_refused(self):
        with self.assertRaises(ProposalError) as caught:
            parse_host("hook", "agent-a", "I could not find the class.")
        self.assertIn("no JSON object", str(caught.exception))

    def test_unparseable_json_is_refused_rather_than_repaired(self):
        with self.assertRaises(ProposalError) as caught:
            parse_host("hook", "agent-a", '{"descriptor": "LX/06X7;",}')
        self.assertIn("unparseable JSON", str(caught.exception))

    def test_a_mapping_is_accepted_as_well_as_text(self):
        proposal = parse_host(
            "hook",
            "agent-a",
            {
                "descriptor": " LX/06X7; ",
                "smali_path": " smali_classes6/X/06X7.smali ",
                "evidence": ["one step"],
            },
        )
        # Stripped, because an agent's whitespace is not part of its answer.
        self.assertEqual(proposal.descriptor, "LX/06X7;")
        self.assertEqual(proposal.smali_path, "smali_classes6/X/06X7.smali")


class SharedParserTests(unittest.TestCase):
    """`parse_proposal` was refactored onto the same helpers. It must not have moved.

    The last-object rule and the unknown-field rejection now have one home each,
    which is the point — a change to either moves both paths rather than leaving
    one of them quietly holding a discipline the other lost. The risk of that is
    the refactor itself, and `proposer.py` had no tests before this file, so the
    behaviour is pinned here on the way past.
    """

    def full(self, descriptor: str = "LX/06X7;", **extra: Any) -> str:
        body: dict[str, Any] = {
            "descriptor": descriptor,
            "smali_path": "smali_classes6/X/06X7.smali",
            "anchor": ["iput-object v13, v1, LX/09rb;->A0H:I"],
            "payload": ["    new-instance v13, Lcom/dfinstagram/SettingsWrapper;"],
            "evidence": ["smali_classes6/X/06X7.smali:412"],
        }
        body.update(extra)
        return json.dumps(body)

    def test_parse_proposal_still_takes_the_last_object(self):
        text = "draft:\n" + self.full("LX/0Di2;") + "\nrevised:\n" + self.full("LX/06X7;")
        self.assertEqual(parse_proposal("hook", "agent-a", text).descriptor, "LX/06X7;")

    def test_parse_proposal_still_refuses_an_unknown_field(self):
        with self.assertRaises(ProposalError) as caught:
            parse_proposal("hook", "agent-a", self.full(confidence=0.9))
        self.assertIn("unknown field(s)", str(caught.exception))
        self.assertIn("confidence", str(caught.exception))

    def test_parse_proposal_still_refuses_a_missing_field(self):
        body = json.loads(self.full())
        del body["payload"]
        with self.assertRaises(ProposalError) as caught:
            parse_proposal("hook", "agent-a", json.dumps(body))
        self.assertIn("omitted required field(s)", str(caught.exception))

    def test_parse_proposal_still_reads_a_whole_answer(self):
        proposal = parse_proposal("hook", "agent-a", self.full())
        self.assertEqual(proposal.descriptor, "LX/06X7;")
        self.assertEqual(len(proposal.anchor), 1)
        self.assertEqual(proposal.evidence, ("smali_classes6/X/06X7.smali:412",))
        # `smali_path` is in the schema and is not a `Proposal` field; it must not
        # have started raising as an unknown one.
        self.assertEqual(proposal.rationale, "smali_classes6/X/06X7.smali:412")


class HostAgreementTests(unittest.TestCase):
    """Agreement over the descriptor alone, and never from one voice."""

    def test_two_distinct_proposers_reaching_one_host_agree(self):
        agreement = host_agreement(
            [make_host(proposer="agent-a"), make_host(proposer="agent-b")]
        )
        self.assertTrue(agreement.agreed)
        self.assertEqual(agreement.agreed_descriptor, "LX/06X7;")
        self.assertEqual(len(agreement.group), 2)
        self.assertEqual(agreement.distinct_answers, 1)
        self.assertIn("2 of 2 distinct proposers", agreement.reason)

    def test_one_proposer_answering_twice_is_not_consensus(self):
        """The cheapest attack on an agreement check is to re-run the same agent.

        It needs no malice — a retry loop does it by accident. The holdout's
        dangerous proposer was fluent, confident, and wrong about its
        justification; three copies of it are still one voice.
        """
        agreement = host_agreement([make_host(proposer="agent-a") for _ in range(3)])
        self.assertFalse(agreement.agreed)
        self.assertIsNone(agreement.agreed_descriptor)
        self.assertEqual(len(agreement.votes), 1)
        self.assertIn("only one proposer answered", agreement.reason)
        self.assertIn("nothing to corroborate", agreement.reason)

    def test_a_single_proposer_is_told_apart_from_genuine_ambiguity(self):
        # The two want opposite responses: find another proposer, versus have a
        # human read the disagreement.
        lonely = host_agreement([make_host()])
        self.assertIn("only one proposer answered", lonely.reason)
        self.assertNotIn("ambiguity", lonely.reason)

        split = host_agreement(
            [
                make_host(proposer="agent-a", descriptor="LX/06X7;"),
                make_host(proposer="agent-b", descriptor="LX/0Di2;"),
            ]
        )
        self.assertFalse(split.agreed)
        self.assertIn("1 of 2 distinct proposers", split.reason)
        self.assertIn("genuine ambiguity", split.reason)
        self.assertIn("rather than being broken by ranking", split.reason)
        self.assertEqual(split.distinct_answers, 2)

    def test_two_of_three_is_agreement_and_matches_the_holdout_result(self):
        agreement = host_agreement(
            [
                make_host(proposer="agent-a", descriptor="LX/06X7;"),
                make_host(proposer="agent-b", descriptor="LX/06X7;"),
                make_host(proposer="agent-c", descriptor="LX/0Di2;"),
            ]
        )
        self.assertTrue(agreement.agreed)
        self.assertEqual(agreement.agreed_descriptor, "LX/06X7;")
        self.assertEqual(agreement.distinct_answers, 2)

    def test_a_plurality_below_the_threshold_does_not_agree(self):
        # Two of five is weak corroboration even though it is the largest group.
        agreement = host_agreement(
            [
                make_host(proposer="agent-a", descriptor="LX/06X7;"),
                make_host(proposer="agent-b", descriptor="LX/06X7;"),
                make_host(proposer="agent-c", descriptor="LX/0Di2;"),
                make_host(proposer="agent-d", descriptor="LX/0Di3;"),
                make_host(proposer="agent-e", descriptor="LX/0Di4;"),
            ]
        )
        self.assertFalse(agreement.agreed)
        self.assertIn("below the 50% agreement threshold", agreement.reason)
        # The plurality is still reported, just not as a decision.
        self.assertEqual(len(agreement.group), 2)

    def test_the_plurality_group_is_reachable_but_never_named_as_a_decision(self):
        # `agreed_descriptor` is the only field naming a class, and it is None
        # here: a caller cannot read a winner out of this without seeing the count.
        agreement = host_agreement([make_host()])
        self.assertIsNone(agreement.agreed_descriptor)
        self.assertEqual(agreement.group[0].descriptor, "LX/06X7;")

    def test_no_proposals_agree_about_nothing(self):
        agreement = host_agreement([])
        self.assertFalse(agreement.agreed)
        self.assertEqual(agreement.group, ())
        self.assertEqual(agreement.distinct_answers, 0)
        self.assertIn("no host proposals", agreement.reason)

    def test_a_tie_is_broken_deterministically_and_still_does_not_agree(self):
        pair = [
            make_host(proposer="agent-a", descriptor="LX/06X7;"),
            make_host(proposer="agent-b", descriptor="LX/0Di2;"),
        ]
        first = host_agreement(pair)
        second = host_agreement(list(reversed(pair)))
        self.assertEqual(first.group[0].descriptor, second.group[0].descriptor)
        self.assertFalse(first.agreed)
        self.assertFalse(second.agreed)

    def test_evidence_and_alternatives_are_not_part_of_the_identity(self):
        # Prose agreement between language models is close to worthless as
        # corroboration; the same class reached twice is not.
        agreement = host_agreement(
            [
                make_host(proposer="agent-a", evidence=("the label id is the Options string",)),
                make_host(proposer="agent-b", evidence=("reached it from the AP1 caller graph",)),
            ]
        )
        self.assertTrue(agreement.agreed)

    def test_the_ledger_records_a_host_agreement_when_it_is_told_what_was_asked(self):
        """The recipe `host_agreement`'s docstring gives, pinned by a test.

        This replaces a test that measured the gap: `agreement_claim` used to
        count a proposal as having answered only when it named a descriptor AND a
        non-empty anchor, so two proposers who agreed on a host tallied as zero
        answered and the claim came back `not_exercised`. It failed safe — every
        by-agent hook simply stalled at the gate — which is exactly the kind of
        failure that goes unnoticed until a port is blocked on it.

        Both halves of the recipe are load-bearing, so both are asserted: the
        votes rather than the raw proposals, and the shape of the question.
        """
        agreeing = [make_host(proposer="agent-a"), make_host(proposer="agent-b")]
        agreement = host_agreement(agreeing)
        self.assertTrue(agreement.agreed)

        claim = agreement_claim(
            "hook", [item.to_dict() for item in agreement.votes], asked=HOST_ONLY
        )
        self.assertIs(claim.verdict, Verdict.PASSED)
        self.assertTrue(claim.verdict.satisfies)
        self.assertEqual(claim.detail["agreed"], 2)
        self.assertEqual(claim.detail["asked"], "host")

        # Told nothing, it still judges by the whole-patch shape, and a host
        # proposal has not answered that question. The widening is something a
        # call site says, never something inferred from the proposals.
        default = agreement_claim("hook", [item.to_dict() for item in agreement.votes])
        self.assertIs(default.verdict, Verdict.NOT_EXERCISED)
        self.assertIn("both a host and an anchor", default.summary)

    def test_the_votes_are_what_the_claim_must_be_given(self):
        """The other half of the recipe, and the one with a failure mode.

        `agreement_claim` counts what it is handed. Given the raw proposals, one
        agent run three times files `proposer_agreement` as `passed` reading "3
        of 3", and the ledger — the artifact a human reads and the thing
        `readiness()` consults — records a consensus that never existed.
        """
        repeated = [make_host(proposer="agent-a") for _ in range(3)]
        agreement = host_agreement(repeated)
        self.assertFalse(agreement.agreed)

        honest = agreement_claim(
            "hook", [item.to_dict() for item in agreement.votes], asked=HOST_ONLY
        )
        self.assertIsNot(honest.verdict, Verdict.PASSED)
        self.assertEqual(honest.detail["proposals"], 1)

        forged = agreement_claim(
            "hook", [item.to_dict() for item in repeated], asked=HOST_ONLY
        )
        self.assertIs(forged.verdict, Verdict.PASSED)
        self.assertEqual(forged.detail["agreed"], 3)

    def test_one_per_proposer_is_the_single_place_the_collapse_happens(self):
        # Shared with `assess`, so a mutation that lets one voice count twice
        # breaks both paths rather than leaving one of them quietly unguarded.
        proposals = [make_host(proposer="agent-a") for _ in range(3)]
        self.assertEqual(len(one_per_proposer(proposals)), 1)


class BlindHoldoutTests(unittest.TestCase):
    """The prompt must measure the agent, not hand it the previous port's answer.

    Every assertion here runs against the REAL manifest, because the thing being
    guarded is what a real run would show a real proposer.
    """

    def hooks(self) -> list[Hook]:
        return load_manifest(MANIFEST)

    def test_the_host_prompt_shows_intent_and_intent_constraints(self):
        for hook in self.hooks():
            with self.subTest(hook=hook.hook_id):
                prompt = host_prompt(hook, SANDBOX, "439")
                self.assertIn(hook.intent, prompt)
                for item in hook.intent_constraints:
                    self.assertIn(item, prompt)

    def test_the_host_prompt_never_shows_a_proposer_hook_constraints(self):
        """`constraints` is what LAST version's patch looked like.

        "the five-line anchor including the drawable and label is what
        disambiguates" gives away the anchor's shape; another entry names the
        register the previous port used (p3 on 430, p4 on 439) and the
        self-profile type it compared against. The shape is exactly what changes
        between versions, so any of it in the prompt turns a search into a
        reading-comprehension exercise and makes every agreement number this
        pipeline reports meaningless.

        The positive control matters as much as the assertion: `assertNotIn` over
        strings the prompt could never contain would pass no matter what the code
        did. So the same strings are moved into `intent_constraints` and shown to
        arrive.
        """
        checked = 0
        for hook in self.hooks():
            with self.subTest(hook=hook.hook_id):
                prompt = host_prompt(hook, SANDBOX, "439")
                for item in hook.constraints:
                    self.assertNotIn(item, prompt)
                    checked += 1
                # Positive control: these exact strings DO reach the prompt when
                # they are the ones it is supposed to carry.
                leaked = host_prompt(
                    replace(hook, intent_constraints=hook.constraints), SANDBOX, "439"
                )
                for item in hook.constraints:
                    self.assertIn(item, leaked)
        self.assertGreater(checked, 0, "no hook carries constraints; nothing was tested")

    def test_no_class_named_in_the_constraints_reaches_the_host_prompt(self):
        """Whole-string absence is not enough: one descriptor is the whole answer.

        A constraint that mentioned `LX/077N;` would still hand over a real class
        from the previous port even if the sentence around it were reworded.
        """
        checked = 0
        for hook in self.hooks():
            with self.subTest(hook=hook.hook_id):
                prompt = host_prompt(hook, SANDBOX, "439")
                named = set(DESCRIPTOR_IN_TEXT.findall(" ".join(hook.constraints)))
                for descriptor in sorted(named):
                    self.assertNotIn(descriptor, prompt)
                    checked += 1
                leaked = host_prompt(
                    replace(hook, intent_constraints=hook.constraints), SANDBOX, "439"
                )
                for descriptor in sorted(named):
                    self.assertIn(descriptor, leaked)
        self.assertGreater(checked, 0, "no constraint names a class; nothing was tested")

    def test_the_host_prompt_asks_for_no_patch_at_all(self):
        prompt = host_prompt(make_hook(), SANDBOX, "439")
        self.assertIn("You do NOT need to write the patch", prompt)
        self.assertNotIn('"anchor"', prompt)
        self.assertNotIn('"payload"', prompt)
        # Nor the manifest's own anchor pattern or payload template, which are the
        # answer's shape written down.
        for line in make_hook().anchor + make_hook().payload:
            self.assertNotIn(line, prompt)

    def test_the_host_prompt_does_not_carry_the_idempotence_marker(self):
        # The marker is a requirement of the applier, not of the app. It belongs
        # in a payload, and the host path asks for no payload.
        self.assertNotIn(make_hook().marker, host_prompt(make_hook(), SANDBOX, "439"))

    def test_the_host_prompt_keeps_the_sandbox_framing(self):
        prompt = host_prompt(make_hook(), SANDBOX, "439")
        self.assertIn(str(SANDBOX), prompt)
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("HARDLINKED", prompt)
        self.assertIn("Do not read anything under any git repository", prompt)
        self.assertIn("recorded answer exists elsewhere on this machine", prompt)

    def test_the_host_prompt_keeps_the_two_warnings_a_human_porter_would_hold(self):
        prompt = host_prompt(make_hook(), SANDBOX, "439")
        # Names are recycled: a familiar descriptor is evidence of nothing.
        self.assertIn("RECYCLED between versions", prompt)
        # And the 430 settings hook was inert on half of devices because a
        # MobileConfig flag chose the other implementation.
        self.assertIn("MORE THAN ONE implementation", prompt)

    def test_the_version_label_reaches_the_prompt(self):
        self.assertIn("Instagram 430", host_prompt(make_hook(), SANDBOX, "430"))

    def test_a_hook_with_no_intent_constraints_still_produces_a_usable_prompt(self):
        prompt = host_prompt(make_hook(intent_constraints=()), SANDBOX, "439")
        self.assertIn("(none beyond the description above)", prompt)


class HostVerifierPromptTests(unittest.TestCase):
    """The verifier gets the claim. Not the story that produced it."""

    def subject(self) -> HostProposal:
        return make_host(
            evidence=("I matched the label id against res/values/strings.xml",),
            alternatives=("LX/0Di2; only logs",),
            unresolved=("could not identify the selector flag",),
        )

    def test_it_carries_the_class_and_the_behaviour(self):
        prompt = host_verifier_prompt(make_hook(), self.subject(), SANDBOX)
        self.assertIn("LX/06X7;", prompt)
        self.assertIn("smali_classes6/X/06X7.smali", prompt)
        self.assertIn(make_hook().intent, prompt)
        self.assertIn(str(SANDBOX), prompt)

    def test_it_never_carries_the_proposers_reasoning(self):
        """A verifier shown a fluent rationale checks the rationale and agrees.

        One holdout proposer justified a correct answer with a fabricated claim
        about register state. A reviewer reading both would have been reassured by
        exactly the wrong thing.
        """
        subject = self.subject()
        prompt = host_verifier_prompt(make_hook(), subject, SANDBOX)
        for hidden in subject.evidence + subject.alternatives + subject.unresolved:
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, prompt)
        self.assertNotIn(subject.proposer, prompt)

    def test_it_defaults_to_refuted(self):
        prompt = host_verifier_prompt(make_hook(), self.subject(), SANDBOX)
        self.assertIn("REFUTE", prompt)
        self.assertIn("Default to `refuted: true`", prompt)
        self.assertIn('"refuted"', prompt)

    def test_it_asks_about_the_class_and_not_about_a_patch(self):
        prompt = host_verifier_prompt(make_hook(), self.subject(), SANDBOX)
        self.assertIn("No patch is being claimed", prompt)
        self.assertIn("another implementation of the same user-facing control", prompt)

    def test_a_proposal_with_no_path_still_produces_a_prompt(self):
        prompt = host_verifier_prompt(make_hook(), make_host(smali_path=""), SANDBOX)
        self.assertIn("LX/06X7;", prompt)
        self.assertNotIn("()", prompt)


class CollectHostsTests(unittest.TestCase):
    """k proposers, blind of each other, and a failure is a drop and never a retry."""

    def test_every_proposer_gets_the_same_prompt_and_sees_no_other_answer(self):
        """Independence is a property of this loop, not of the agents.

        A runtime that threads one session through k invocations conditions
        proposer k on proposers 1..k-1, and that surfaces as *agreement* rather
        than as an error — which is why the prompt is built once, before the loop,
        and never accumulates.
        """
        runners = {
            "agent-a": Recorder(answer("LX/06X7;")),
            "agent-b": Recorder(answer("LX/0Di2;")),
            "agent-c": Recorder(answer("LX/06X7;")),
        }
        run = collect_hosts(make_hook(), SANDBOX, "439", runners)
        given = [recorder.prompts[0] for recorder in runners.values()]
        self.assertEqual(len(set(given)), 1)
        self.assertEqual(given[0], host_prompt(make_hook(), SANDBOX, "439"))
        for prompt in given:
            for other in runners.values():
                self.assertNotIn(str(other.reply), prompt)
                self.assertNotIn("LX/0Di2;", prompt)
        self.assertEqual(len(run.proposals), 3)

    def test_a_proposer_that_does_not_parse_is_dropped_and_k_shrinks(self):
        """k-1 real answers is a smaller sample; a retried agent is a correlated one.

        The run must not fail either: one agent inventing a schema field is
        exactly what happened on the first real run, and the other two answers
        were the measurement.
        """
        runners = {
            "agent-a": Recorder(answer("LX/06X7;")),
            "agent-b": Recorder("I could not work it out."),
            "agent-c": Recorder(answer("LX/06X7;")),
        }
        run = collect_hosts(make_hook(), SANDBOX, "439", runners)
        self.assertEqual([item.proposer for item in run.proposals], ["agent-a", "agent-c"])
        self.assertEqual(len(run.failures), 1)
        self.assertIn("agent-b", run.failures[0])
        self.assertIn("ProposalError", run.failures[0])
        # It was asked once and never asked again.
        self.assertEqual(len(runners["agent-b"].prompts), 1)
        # And the surviving two still corroborate each other: k shrank, the
        # measurement survived.
        self.assertTrue(host_agreement(run.proposals).agreed)

    def test_a_proposer_that_raises_is_dropped_the_same_way(self):
        runners = {
            "agent-a": Recorder(answer("LX/06X7;")),
            "agent-b": Recorder(RuntimeError("the agent runtime died")),
        }
        run = collect_hosts(make_hook(), SANDBOX, "439", runners)
        self.assertEqual(len(run.proposals), 1)
        self.assertIn("RuntimeError", run.failures[0])
        self.assertIn("the agent runtime died", run.failures[0])

    def test_every_proposer_failing_leaves_a_run_with_no_answers_rather_than_a_crash(self):
        run = collect_hosts(
            make_hook(), SANDBOX, "439", {"agent-a": Recorder("nothing at all")}
        )
        self.assertEqual(run.proposals, ())
        self.assertEqual(len(run.failures), 1)
        self.assertFalse(host_agreement(run.proposals).agreed)

    def test_the_verifier_is_asked_about_the_plurality_answer(self):
        verifier = Recorder('{"refuted": false, "finding": "could not break it", "checked": []}')
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {
                "agent-a": Recorder(answer("LX/06X7;")),
                "agent-b": Recorder(answer("LX/06X7;")),
                "agent-c": Recorder(answer("LX/0Di2;")),
            },
            verifiers={"verifier-x": verifier},
        )
        self.assertEqual(len(run.refutations), 1)
        self.assertFalse(run.refutations[0].refuted)
        self.assertIn("LX/06X7;", verifier.prompts[0])
        self.assertNotIn("LX/0Di2;", verifier.prompts[0])

    def test_a_verifier_that_also_proposed_is_skipped_and_said_to_be(self):
        # Not independent evidence, and the ledger would refuse its claim anyway.
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {"agent-a": Recorder(answer()), "agent-b": Recorder(answer())},
            verifiers={"agent-a": Recorder('{"refuted": false, "finding": "fine"}')},
        )
        self.assertEqual(run.refutations, ())
        self.assertIn("skipped as verifier because it also proposed", run.failures[0])

    def test_a_verifier_that_produces_nothing_usable_has_refuted(self):
        # Failing closed: "I could not check" must not read as "it is fine".
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {"agent-a": Recorder(answer()), "agent-b": Recorder(answer())},
            verifiers={"verifier-x": Recorder("I ran out of turns.")},
        )
        self.assertTrue(run.refutations[0].refuted)

    def test_a_verifier_that_raises_is_recorded_as_refuting(self):
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {"agent-a": Recorder(answer()), "agent-b": Recorder(answer())},
            verifiers={"verifier-x": Recorder(RuntimeError("boom"))},
        )
        self.assertTrue(run.refutations[0].refuted)
        self.assertIn("verifier failed: RuntimeError", run.refutations[0].finding)

    def test_no_verifier_runs_when_nothing_parsed(self):
        verifier = Recorder('{"refuted": false, "finding": "fine"}')
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {"agent-a": Recorder("no json here")},
            verifiers={"verifier-x": verifier},
        )
        self.assertEqual(verifier.prompts, [])
        self.assertEqual(run.refutations, ())

    def test_the_run_serialises_for_a_report(self):
        run = collect_hosts(
            make_hook(),
            SANDBOX,
            "439",
            {"agent-a": Recorder(answer()), "agent-b": Recorder(answer())},
            verifiers={"verifier-x": Recorder('{"refuted": true, "finding": "wrong class"}')},
        )
        data = run.to_dict()
        json.dumps(data)
        self.assertEqual(data["hook_id"], "install_settings_long_click_actionbar")
        self.assertEqual(len(data["proposals"]), 2)
        self.assertTrue(data["refutations"][0]["refuted"])
        self.assertIsInstance(run, HostRun)


class MutationTests(unittest.TestCase):
    """Delete a guard, show what ships.

    The other three mutations this path needs — reverting the last-object rule,
    leaking `constraints` into the prompt, and dropping the unknown-field
    rejection — were proved on an out-of-tree copy of the package rather than by
    patching in process, because each of them lives in a module-level function
    that a `mock.patch` would not reach from the call sites that matter. This one
    is kept in process because `one_per_proposer` is the single shared collapse
    and patching it is exactly how `test_proposals.py` proves the same guard.
    """

    def test_without_the_collapse_one_agent_manufactures_its_own_consensus(self):
        """Removing it: re-running one agent three times clears the agreement gate.

        This needs no malice — a retry loop does it by accident, and an agent
        runtime that reuses a session id does it invisibly. The holdout's
        dangerous proposer was fluent, confident and wrong; three copies of it
        would be reported here as three independent corroborations.
        """
        proposals = [make_host(proposer="agent-a") for _ in range(3)]
        baseline = host_agreement(proposals)
        self.assertFalse(baseline.agreed)
        self.assertIn("only one proposer answered", baseline.reason)

        with mock.patch("dfinsta_pipeline.proposals.one_per_proposer", side_effect=list):
            forged = host_agreement(proposals)
        self.assertTrue(forged.agreed)
        self.assertEqual(forged.agreed_descriptor, "LX/06X7;")
        self.assertIn("3 of 3 distinct proposers", forged.reason)


if __name__ == "__main__":
    unittest.main()

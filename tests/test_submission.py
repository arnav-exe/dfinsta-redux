"""Tests for the trusted submission client — the only way a human answers a gate.

The module's governing rule is one sentence: **the client re-derives the gate
subject from recorded state and refuses to let a human sign a hash it cannot
reproduce.** Almost every test here is an attack on one of the three ways that
rule can quietly stop holding.

**A published hash can slip past unchecked.** `verify_published_gate` compares
six fields, and a seventh added to `DerivedSubject` without a comparison would be
a value a human signs that nobody verified. `test_every_derived_field_but_the_actor_is_compared`
is therefore bound to `dataclasses.fields`, not to a list of six names: adding a
field to the dataclass makes the test fail until someone decides whether it is
compared here or, like `allowed_actor`, somewhere it can be named.

**A published hash can reach the decision.** `assemble_decision` takes no
`GateRequest`, which is what makes the copy-the-Workflow's-hashes path not exist
rather than exist and be avoided. That is asserted against the signature, because
a behavioural test cannot observe an argument nobody passes.

**Two different decisions can share an identity.** Every field of `GateDecision`
except the schema tag and the two ids feeds the digest. If a field were added and
forgotten, two materially different decisions would mint the same
`idempotency_id`, and the second would be deduplicated into the first by Temporal
— a human's `reject` silently answered by an earlier `approve`.
`test_the_identity_covers_every_decision_field_but_the_excluded_ones` recomputes
the identity from the *decision's own* fields, so it fails on a subset and on a
superset, and carries positive controls proving both directions bite.

The stale-approval defence in `read_journal` gets its own named test for the same
reason: a cached answer applied to a re-raised gate is the exact failure the hash
chain exists to prevent, arriving through the client's own cache.

Not covered here, by arrangement: `read_pending_gate`, `submit_answer`, `_run`
and `main`.
"""

import dataclasses
import inspect
import json
import os
import stat
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from dfinsta_pipeline.contracts import ID_PATTERN, GateDecision, GateRequest, canonical_json
from dfinsta_pipeline.submission import (
    CONFIRMATION_LENGTH,
    DECISION_ID_PREFIX,
    GATE_KINDS,
    IDEMPOTENCY_ID_PREFIX,
    IDENTITY_EXCLUDED_FIELDS,
    REPLAY_VERIFICATION_GATE,
    VERDICTS,
    Answer,
    DerivedSubject,
    JournalEntry,
    PendingGate,
    Principal,
    SubmissionRefused,
    assemble_decision,
    check_confirmation,
    decision_identity,
    describe,
    gate_request_from_dict,
    journal_path,
    load_principal,
    read_journal,
    select_gate_kind,
    verify_published_gate,
    write_journal,
)


# --------------------------------------------------------------------- fixture

RUN_ID = "port-439-replay"
WORKFLOW_ID = "port-439-replay-workflow"
ACTOR = "human-sam"
POLICY = "policy-2026-08"

# `replay_gate.GATE_ID_SUFFIX`, spelled out rather than imported: `replay_gate`
# pulls in the ledger, and this suite has no business opening a database. The
# duplication is the point of `test_the_replay_gate_is_recognised_by_its_run_scoped_id`
# — if the producer's suffix moved, the client's matcher would stop recognising
# its own gate, and only a test that names both spellings catches that.
GATE_ID = f"{RUN_ID}-final-verification-gate"

# Three *distinct* digests where production uses one. `ReplayRunWorkflow` binds
# the same request hash three times over, so a fixture that reused one value
# would read a decision that copied `subject` into `admission` as correct.
SUBJECT_SHA = "abcdef0123456789" * 4
ADMISSION_SHA = "0123456789abcdef" * 4
PREPARED_SHA = "fedcba9876543210" * 4

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
ISSUED_AT = (NOW - timedelta(minutes=1)).isoformat()
EXPIRES_AT = (NOW + timedelta(hours=1)).isoformat()

#: One alternative per compared field of `DerivedSubject`. Bound to the dataclass
#: in `test_every_derived_field_but_the_actor_is_compared`, so a new field forces
#: a decision here rather than passing unnoticed.
FIELD_ALTERNATIVES = {
    "run_id": "port-440-replay",
    "gate_id": "port-440-replay-final-verification-gate",
    "subject_sha256": "1" * 64,
    "admission_sha256": "2" * 64,
    "prepared_sha256": "3" * 64,
    "policy_revision": "policy-2026-09",
}


def make_request(**overrides: Any) -> GateRequest:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "subject_sha256": SUBJECT_SHA,
        "admission_sha256": ADMISSION_SHA,
        "prepared_sha256": PREPARED_SHA,
        "policy_revision": POLICY,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
    }
    fields.update(overrides)
    return GateRequest(**fields)


def make_derived(**overrides: Any) -> DerivedSubject:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "gate_id": GATE_ID,
        "subject_sha256": SUBJECT_SHA,
        "admission_sha256": ADMISSION_SHA,
        "prepared_sha256": PREPARED_SHA,
        "policy_revision": POLICY,
        "allowed_actor": ACTOR,
    }
    fields.update(overrides)
    return DerivedSubject(**fields)


def make_principal(actor: str = ACTOR) -> Principal:
    return Principal(1, os.geteuid(), actor)


def make_answer(verdict: str = "approve", rationale: str = "Replay matched byte for byte") -> Answer:
    return Answer(verdict, rationale)  # type: ignore[arg-type]


def make_pending(**overrides: Any) -> PendingGate:
    return PendingGate(WORKFLOW_ID, REPLAY_VERIFICATION_GATE, make_request(), make_derived(**overrides))


def make_decision() -> GateDecision:
    return assemble_decision(make_derived(), make_principal(), make_answer(), NOW)


class TemporaryRootTestCase(unittest.TestCase):
    """One throwaway directory per test; nothing here touches a real state root."""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)


# ------------------------------------------------------------------- principal


class LoadPrincipalTests(TemporaryRootTestCase):
    """The actor is what the operating system already decided, not a flag."""

    def write_principal(
        self, name: str = "principal.json", *, mode: int = 0o600, **overrides: Any
    ) -> Path:
        document: dict[str, Any] = {"schema_version": 1, "uid": os.geteuid(), "actor": ACTOR}
        document.update(overrides)
        return self.write_raw(name, json.dumps(document), mode=mode)

    def write_raw(self, name: str, body: str, *, mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path

    def refusal(self, path: Path, *, effective_uid: int | None = None) -> str:
        uid = os.geteuid() if effective_uid is None else effective_uid
        with self.assertRaises(SubmissionRefused) as raised:
            load_principal(path, effective_uid=uid)
        return str(raised.exception)

    def test_a_0600_file_owned_by_this_uid_names_the_actor(self) -> None:
        path = self.write_principal()
        self.assertEqual(
            load_principal(path, effective_uid=os.geteuid()),
            Principal(1, os.geteuid(), ACTOR),
        )

    def test_the_path_must_be_a_path(self) -> None:
        path = self.write_principal()
        with self.assertRaises(SubmissionRefused) as raised:
            load_principal(str(path), effective_uid=os.geteuid())  # type: ignore[arg-type]
        self.assertEqual(str(raised.exception), "Principal path must be a Path")

    def test_a_missing_file_says_how_to_create_it(self) -> None:
        """The message is the whole onboarding path; a bare ENOENT strands a human."""
        missing = self.root / "absent.json"
        self.assertEqual(
            self.refusal(missing),
            f"No principal file at {missing}. Create it, mode 0600, holding "
            '{"schema_version": 1, "uid": <your uid>, "actor": "<actor>"}',
        )

    def test_a_directory_is_not_a_principal(self) -> None:
        directory = self.root / "principal.json"
        directory.mkdir(mode=0o700)
        self.assertEqual(
            self.refusal(directory), f"Principal path is not a regular file: {directory}"
        )

    def test_a_symlink_to_a_perfectly_good_principal_is_still_refused(self) -> None:
        """`lstat` sees the link, so a symlink is refused however good its target.

        This is the right call rather than an oversight: a symlink's own mode is
        0777 and its own owner is whoever made it, so following it would mean
        checking the mode of one file and reading another. Refusing the
        indirection outright is the only version of this check that means
        anything. The message is thin — a human who symlinked their config into a
        dotfiles repo is told "not a regular file" about something that plainly
        is one — but the behaviour is correct.
        """
        target = self.write_principal("real.json")
        link = self.root / "link.json"
        link.symlink_to(target)
        self.assertTrue(link.is_file(), "the target is readable; only the link is refused")
        self.assertEqual(self.refusal(link), f"Principal path is not a regular file: {link}")

    def test_a_file_owned_by_another_uid_is_refused(self) -> None:
        """Exercised through `effective_uid`, which is why it is a parameter.

        Arranging a differently-owned file needs root, and a test that shelled
        out to `sudo` would prove less than this one: the injected uid drives the
        same comparison production drives with `os.geteuid()`.
        """
        path = self.write_principal()
        other = os.geteuid() + 1
        self.assertEqual(
            self.refusal(path, effective_uid=other),
            f"Principal file is not owned by uid {other}: {path}",
        )

    def test_any_group_or_other_bit_is_refused(self) -> None:
        for mode in (0o640, 0o604, 0o666, 0o644, 0o660, 0o601, 0o610):
            with self.subTest(mode=f"{mode:04o}"):
                path = self.write_principal(f"m{mode:o}.json", mode=mode)
                self.assertEqual(
                    self.refusal(path),
                    f"Principal file must have no group or other permissions, "
                    f"found {mode:04o}: {path}",
                )

    def test_owner_only_modes_are_accepted_including_ones_the_message_does_not_name(
        self,
    ) -> None:
        """The check is `mode & 0o077`, so 0400, 0500 and 0700 all pass.

        The message says "must be mode 0600" and the code means "no group or
        other bit". 0400 (read-only, a reasonable thing for a human to set) and
        0700 (an owner-execute bit, which is meaningless on a JSON file and
        harmless) are both accepted. Asserting the code rather than the message
        because the code is right — an execute bit is not a disclosure — but the
        message misdescribes it.
        """
        for mode in (0o600, 0o400, 0o500, 0o700):
            with self.subTest(mode=f"{mode:04o}"):
                path = self.write_principal(f"m{mode:o}.json", mode=mode)
                self.assertEqual(
                    load_principal(path, effective_uid=os.geteuid()).actor, ACTOR
                )

    def test_unparseable_json_is_refused(self) -> None:
        path = self.write_raw("principal.json", "{not json")
        self.assertEqual(self.refusal(path), f"Principal file is not readable JSON: {path}")

    def test_json_that_is_not_an_object_is_refused(self) -> None:
        for body in ("[1, 2]", '"human-sam"', "null", "1"):
            with self.subTest(body=body):
                path = self.write_raw(f"p{abs(hash(body))}.json", body)
                self.assertEqual(
                    self.refusal(path), "principal must be an object with string keys"
                )

    def test_an_unknown_field_is_refused(self) -> None:
        """A field the client does not understand may be the one that mattered."""
        path = self.write_principal(role="release-manager")
        self.assertEqual(self.refusal(path), "Unknown principal field: role")

    def test_a_missing_field_is_refused(self) -> None:
        path = self.write_raw(
            "principal.json", json.dumps({"schema_version": 1, "uid": os.geteuid()})
        )
        self.assertEqual(self.refusal(path), "Missing principal field: actor")

    def test_a_file_naming_another_uid_is_refused_even_when_this_uid_owns_it(self) -> None:
        """Ownership and the claim inside must agree; either alone is forgeable."""
        path = self.write_principal(uid=os.geteuid() + 1)
        self.assertEqual(
            self.refusal(path),
            f"Principal file names uid {os.geteuid() + 1} but this process runs as {os.geteuid()}",
        )

    def test_a_non_integer_uid_is_refused(self) -> None:
        path = self.write_principal(uid=str(os.geteuid()))
        self.assertEqual(self.refusal(path), "Principal uid must be a non-negative integer")

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        for version in (2, 0, "1", None):
            with self.subTest(version=version):
                path = self.write_principal(f"s{version}.json", schema_version=version)
                self.assertEqual(self.refusal(path), "Unsupported principal schema")

    def test_an_actor_that_is_not_an_identifier_is_refused(self) -> None:
        """The actor reaches Temporal History and a decision id; it stays a name."""
        for actor in ("human sam", "sam@dfinsta", "", "-leading-dash", "a" * 129, 7):
            with self.subTest(actor=actor):
                path = self.write_principal(f"a{abs(hash(str(actor)))}.json", actor=actor)
                self.assertEqual(self.refusal(path), "Invalid principal actor")


# ------------------------------------------------------------ published request


class GateRequestFromDictTests(unittest.TestCase):
    """The published request is the one input a stale or hostile Workflow owns."""

    def test_a_real_request_round_trips(self) -> None:
        request = make_request()
        data = json.loads(json.dumps(dataclasses.asdict(request)))
        self.assertEqual(gate_request_from_dict(data), request)

    def test_a_non_object_is_refused(self) -> None:
        for value in (None, [1, 2], "gate", 7, {1: "a"}):
            with self.subTest(value=value), self.assertRaises(SubmissionRefused) as raised:
                gate_request_from_dict(value)
            self.assertEqual(
                str(raised.exception), "published gate request must be an object with string keys"
            )

    def test_an_unknown_field_is_refused(self) -> None:
        """An added field is a Workflow this client is too old to understand."""
        data = dataclasses.asdict(make_request())
        with self.assertRaises(SubmissionRefused) as raised:
            gate_request_from_dict({**data, "reviewer": ACTOR})
        self.assertEqual(str(raised.exception), "Unknown published gate request field: reviewer")

    def test_a_missing_field_is_refused(self) -> None:
        data = dataclasses.asdict(make_request())
        with self.assertRaises(SubmissionRefused) as raised:
            gate_request_from_dict({k: v for k, v in data.items() if k != "expires_at"})
        self.assertEqual(str(raised.exception), "Missing published gate request field: expires_at")

    def test_a_structurally_invalid_request_is_refused_rather_than_constructed(self) -> None:
        data = dataclasses.asdict(make_request())
        with self.assertRaises(SubmissionRefused) as raised:
            gate_request_from_dict({**data, "subject_sha256": "not-a-digest"})
        self.assertEqual(
            str(raised.exception),
            "Published gate request is invalid: Invalid gate subject SHA-256",
        )
        for field, value in (
            ("schema_version", 2),
            ("run_id", "-leading-dash"),
            ("gate_id", ""),
            ("policy_revision", "policy 2026"),
            ("issued_at", ""),
            ("expires_at", 7),
        ):
            with self.subTest(field=field), self.assertRaises(SubmissionRefused):
                gate_request_from_dict({**data, field: value})


# --------------------------------------------------------- the central check


class VerifyPublishedGateTests(unittest.TestCase):
    """THE check. A mismatch is never a reason to prefer one side."""

    def setUp(self) -> None:
        self.published = make_request()
        self.derived = make_derived()

    def test_a_gate_matching_the_derived_subject_is_accepted(self) -> None:
        self.assertIsNone(verify_published_gate(self.published, self.derived, now=NOW))

    def test_every_derived_field_but_the_actor_is_compared(self) -> None:
        """Bound to `dataclasses.fields`, so a new field cannot go uncompared.

        Mutation: delete any line from the comparison tuple. That field then
        travels from the Workflow into the decision unverified — a human signing
        a policy revision, or an admission hash, that nothing reproduced. The
        loop runs both directions because a comparison that read the derived
        value on both sides would be a comparison of a value with itself.
        """
        compared = {field.name for field in dataclasses.fields(DerivedSubject)} - {"allowed_actor"}
        self.assertEqual(
            set(FIELD_ALTERNATIVES),
            compared,
            "a field of DerivedSubject has no alternative here: decide whether "
            "verify_published_gate compares it before adding one",
        )
        for name, alternative in FIELD_ALTERNATIVES.items():
            baseline = getattr(self.derived, name)
            self.assertNotEqual(baseline, alternative)
            with self.subTest(field=name, side="published"):
                with self.assertRaises(SubmissionRefused) as raised:
                    verify_published_gate(
                        dataclasses.replace(self.published, **{name: alternative}),
                        self.derived,
                        now=NOW,
                    )
                message = str(raised.exception)
                self.assertIn(repr(alternative), message)
                self.assertIn(repr(baseline), message)
                self.assertIn("refusing to sign an unverified subject", message)
            with self.subTest(field=name, side="derived"):
                with self.assertRaises(SubmissionRefused):
                    verify_published_gate(
                        self.published,
                        dataclasses.replace(self.derived, **{name: alternative}),
                        now=NOW,
                    )

    def test_the_first_disagreement_is_the_one_named(self) -> None:
        """No repair, no preference: the message names a field and stops."""
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(
                dataclasses.replace(
                    self.published, run_id="port-440-replay", policy_revision="policy-2026-09"
                ),
                self.derived,
                now=NOW,
            )
        self.assertEqual(
            str(raised.exception),
            "Published run 'port-440-replay' is not the derived run 'port-439-replay'; "
            "refusing to sign an unverified subject",
        )

    def test_the_allowed_actor_is_checked_at_assembly_rather_than_here(self) -> None:
        """The one derived field this function does not compare, and why.

        `GateRequest` has no `allowed_actor`, so there is nothing here to compare
        it against. `assemble_decision` is where it bites, against the OS
        principal — which is the only place it can, since that is where the
        answering identity first exists.
        """
        self.assertNotIn(
            "allowed_actor", {field.name for field in dataclasses.fields(GateRequest)}
        )
        other = dataclasses.replace(self.derived, allowed_actor="someone-else")
        self.assertIsNone(verify_published_gate(self.published, other, now=NOW))
        with self.assertRaises(SubmissionRefused):
            assemble_decision(other, make_principal(), make_answer(), NOW)

    def test_arguments_must_be_exact_types(self) -> None:
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(dataclasses.asdict(self.published), self.derived, now=NOW)
        self.assertEqual(str(raised.exception), "Published gate must be an exact GateRequest")
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(self.published, dataclasses.asdict(self.derived), now=NOW)
        self.assertEqual(str(raised.exception), "Derived subject must be an exact DerivedSubject")

    def test_a_naive_now_is_refused(self) -> None:
        for value in (NOW.replace(tzinfo=None), "2026-08-01T12:00:00+00:00", None):
            with self.subTest(now=value), self.assertRaises(SubmissionRefused) as raised:
                verify_published_gate(self.published, self.derived, now=value)
            self.assertEqual(str(raised.exception), "Current time must be an aware datetime")

    def test_naive_published_timestamps_are_refused(self) -> None:
        """A timestamp with no offset is a timestamp in an unknown timezone.

        Comparing it against an aware `now` is either a TypeError or, worse, a
        silent assumption about which zone the worker was in.
        """
        naive = NOW.replace(tzinfo=None)
        for field in ("issued_at", "expires_at"):
            with self.subTest(field=field), self.assertRaises(SubmissionRefused) as raised:
                verify_published_gate(
                    make_request(
                        **{field: (naive - timedelta(minutes=1) if field == "issued_at" else naive + timedelta(hours=1)).isoformat()}
                    ),
                    self.derived,
                    now=NOW,
                )
            self.assertEqual(
                str(raised.exception), "Published gate timestamps require a UTC offset"
            )

    def test_unparseable_published_timestamps_are_refused(self) -> None:
        for field in ("issued_at", "expires_at"):
            with self.subTest(field=field), self.assertRaises(SubmissionRefused) as raised:
                verify_published_gate(
                    make_request(**{field: "yesterday"}), self.derived, now=NOW
                )
            self.assertEqual(str(raised.exception), "Published gate timestamps are invalid")

    def test_a_gate_that_expires_before_it_was_issued_is_refused(self) -> None:
        for expires_at in (ISSUED_AT, (NOW - timedelta(hours=2)).isoformat()):
            with self.subTest(expires_at=expires_at), self.assertRaises(
                SubmissionRefused
            ) as raised:
                verify_published_gate(
                    make_request(expires_at=expires_at), self.derived, now=NOW
                )
            self.assertEqual(str(raised.exception), "Published gate expires before it was issued")

    def test_an_expired_gate_is_refused_here_with_the_expiry_in_the_message(self) -> None:
        """Better than letting the validator refuse: the human learns why.

        A submission that reaches an expired gate comes back as an update
        rejection, and the human cannot tell that from a rejected decision.
        """
        expires_at = (NOW - timedelta(minutes=1)).isoformat()
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(
                make_request(issued_at=(NOW - timedelta(hours=1)).isoformat(), expires_at=expires_at),
                self.derived,
                now=NOW,
            )
        self.assertEqual(str(raised.exception), f"Gate expired at {expires_at}")

    def test_expiry_is_refused_at_the_boundary_instant(self) -> None:
        """`now >= expires_at`: the moment of expiry is expired, not the last valid one."""
        expires_at = NOW.isoformat()
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(
                make_request(expires_at=expires_at), self.derived, now=NOW
            )
        self.assertEqual(str(raised.exception), f"Gate expired at {expires_at}")
        # One microsecond of validity left is still validity.
        self.assertIsNone(
            verify_published_gate(
                make_request(expires_at=(NOW + timedelta(microseconds=1)).isoformat()),
                self.derived,
                now=NOW,
            )
        )

    def test_a_gate_from_this_clients_future_is_refused(self) -> None:
        """A gate issued ahead of this clock means one of the two clocks is wrong.

        Deciding anything on that basis is deciding on an unknown expiry too.
        """
        issued_at = (NOW + timedelta(minutes=6)).isoformat()
        with self.assertRaises(SubmissionRefused) as raised:
            verify_published_gate(
                make_request(issued_at=issued_at, expires_at=(NOW + timedelta(hours=2)).isoformat()),
                self.derived,
                now=NOW,
            )
        self.assertEqual(
            str(raised.exception),
            f"Gate was issued at {issued_at}, which is in this client's future; "
            "check the clock before deciding anything",
        )

    def test_a_minute_of_clock_skew_is_allowed(self) -> None:
        """Two hosts are never in exact agreement; five minutes is the allowance."""
        self.assertIsNone(
            verify_published_gate(
                make_request(
                    issued_at=(NOW + timedelta(minutes=1)).isoformat(),
                    expires_at=(NOW + timedelta(hours=2)).isoformat(),
                ),
                self.derived,
                now=NOW,
            )
        )
        # And the boundary itself: exactly five minutes ahead is still inside.
        self.assertIsNone(
            verify_published_gate(
                make_request(
                    issued_at=(NOW + timedelta(minutes=5)).isoformat(),
                    expires_at=(NOW + timedelta(hours=2)).isoformat(),
                ),
                self.derived,
                now=NOW,
            )
        )

    def test_offsets_are_compared_as_instants_rather_than_as_strings(self) -> None:
        """The same instant written in two zones is the same instant."""
        elsewhere = timezone(timedelta(hours=5, minutes=30))
        self.assertIsNone(
            verify_published_gate(
                make_request(
                    issued_at=(NOW - timedelta(minutes=1)).astimezone(elsewhere).isoformat(),
                    expires_at=(NOW + timedelta(hours=1)).astimezone(elsewhere).isoformat(),
                ),
                self.derived,
                now=NOW,
            )
        )


# ---------------------------------------------------------------- human answer


class AnswerTests(unittest.TestCase):
    def test_every_verdict_the_module_names_is_accepted(self) -> None:
        self.assertEqual(VERDICTS, ("approve", "reject", "defer"))
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertEqual(make_answer(verdict).verdict, verdict)

    def test_an_invented_verdict_is_refused(self) -> None:
        for verdict in ("Approve", "yes", "block", "", None):
            with self.subTest(verdict=verdict), self.assertRaises(SubmissionRefused) as raised:
                make_answer(verdict)
            self.assertEqual(str(raised.exception), "Verdict must be one of approve, reject, defer")

    def test_a_decision_without_a_reason_is_refused(self) -> None:
        """An unexplained approval is indistinguishable from a mis-click."""
        for rationale in ("", "   ", "\t\n ", None, 7):
            with self.subTest(rationale=rationale), self.assertRaises(SubmissionRefused) as raised:
                make_answer("approve", rationale)
            self.assertEqual(str(raised.exception), "A decision requires a rationale")

    def test_the_rationale_is_bounded_at_2048_characters(self) -> None:
        self.assertEqual(make_answer("approve", "x" * 2048).rationale, "x" * 2048)
        with self.assertRaises(SubmissionRefused) as raised:
            make_answer("approve", "x" * 2049)
        self.assertEqual(str(raised.exception), "Rationale is longer than 2048 characters")


# -------------------------------------------------------- identity and assembly


class DecisionIdentityTests(unittest.TestCase):
    def test_identical_content_yields_identical_ids(self) -> None:
        """This is what makes a resubmission after a dropped connection a no-op."""
        content = {"actor": ACTOR, "decision": "approve"}
        self.assertEqual(decision_identity(content), decision_identity(dict(content)))

    def test_key_order_does_not_change_the_identity(self) -> None:
        first = {"actor": ACTOR, "decision": "approve", "issued_at": NOW.isoformat()}
        second = {key: first[key] for key in reversed(list(first))}
        self.assertNotEqual(list(first), list(second))
        self.assertEqual(decision_identity(first), decision_identity(second))

    def test_both_ids_carry_the_same_digest_under_their_own_prefixes(self) -> None:
        decision_id, idempotency_id = decision_identity({"actor": ACTOR})
        self.assertTrue(decision_id.startswith(DECISION_ID_PREFIX))
        self.assertTrue(idempotency_id.startswith(IDEMPOTENCY_ID_PREFIX))
        self.assertEqual(
            decision_id[len(DECISION_ID_PREFIX) :], idempotency_id[len(IDEMPOTENCY_ID_PREFIX) :]
        )


class AssembleDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.derived = make_derived()
        self.principal = make_principal()
        self.answer = make_answer()
        self.decision = assemble_decision(self.derived, self.principal, self.answer, NOW)

    def test_the_decision_is_built_from_the_derived_subject(self) -> None:
        self.assertEqual(self.decision.schema_version, 1)
        self.assertEqual(self.decision.actor, ACTOR)
        self.assertEqual(self.decision.run_id, RUN_ID)
        self.assertEqual(self.decision.gate_id, GATE_ID)
        self.assertEqual(self.decision.subject_sha256, SUBJECT_SHA)
        self.assertEqual(self.decision.admission_sha256, ADMISSION_SHA)
        self.assertEqual(self.decision.prepared_sha256, PREPARED_SHA)
        self.assertEqual(self.decision.policy_revision, POLICY)
        self.assertEqual(self.decision.decision, "approve")
        self.assertEqual(self.decision.rationale, self.answer.rationale)
        self.assertEqual(self.decision.issued_at, NOW.isoformat())

    def test_the_decision_validates_as_a_contract(self) -> None:
        """Constructed, not hand-built: `GateDecision.__post_init__` already ran."""
        self.assertEqual(GateDecision.from_dict(dataclasses.asdict(self.decision)), self.decision)
        self.assertEqual(
            canonical_json(self.decision), canonical_json(dataclasses.asdict(self.decision))
        )

    def test_both_ids_are_well_formed_identifiers(self) -> None:
        """They become a Temporal update id and a ledger key; length matters."""
        for value in (self.decision.decision_id, self.decision.idempotency_id):
            with self.subTest(value=value):
                self.assertIsNotNone(ID_PATTERN.fullmatch(value))
        self.assertTrue(self.decision.decision_id.startswith(DECISION_ID_PREFIX))
        self.assertTrue(self.decision.idempotency_id.startswith(IDEMPOTENCY_ID_PREFIX))

    def test_assembling_the_same_answer_twice_yields_the_same_ids(self) -> None:
        again = assemble_decision(make_derived(), make_principal(), make_answer(), NOW)
        self.assertIsNot(again, self.decision)
        self.assertEqual(again, self.decision)
        self.assertEqual(again.decision_id, self.decision.decision_id)
        self.assertEqual(again.idempotency_id, self.decision.idempotency_id)

    def test_any_change_to_the_answer_or_the_subject_changes_the_ids(self) -> None:
        """Nothing that distinguishes two decisions may be absent from the digest."""
        variants = {
            "rationale": assemble_decision(
                self.derived, self.principal, make_answer("approve", "different reason"), NOW
            ),
            "verdict": assemble_decision(
                self.derived, self.principal, make_answer("reject"), NOW
            ),
            "issued_at": assemble_decision(
                self.derived, self.principal, self.answer, NOW + timedelta(seconds=1)
            ),
            "actor": assemble_decision(
                dataclasses.replace(self.derived, allowed_actor="human-alex"),
                make_principal("human-alex"),
                self.answer,
                NOW,
            ),
            **{
                name: assemble_decision(
                    dataclasses.replace(self.derived, **{name: alternative}),
                    self.principal,
                    self.answer,
                    NOW,
                )
                for name, alternative in FIELD_ALTERNATIVES.items()
            },
        }
        for label, variant in variants.items():
            with self.subTest(changed=label):
                self.assertNotEqual(variant.decision_id, self.decision.decision_id)
                self.assertNotEqual(variant.idempotency_id, self.decision.idempotency_id)

    def test_the_identity_covers_every_decision_field_but_the_excluded_ones(self) -> None:
        """THE DRIFT TEST. Recomputed from the decision's own fields, not the source.

        Mutation: add a field to `GateDecision` and forget it here, or drop one
        from the content dict. Either way two materially different decisions can
        mint the same `idempotency_id` — and Temporal deduplicates the second
        into the first, so a human's `reject` is answered by an earlier
        `approve` and the receipt says it worked.

        The digest changes on a subset and on a superset, so this assertion is
        an exact cover, and the two loops below are its positive controls.
        """
        names = {field.name for field in dataclasses.fields(GateDecision)}
        self.assertEqual(
            IDENTITY_EXCLUDED_FIELDS, {"schema_version", "decision_id", "idempotency_id"}
        )
        self.assertLessEqual(IDENTITY_EXCLUDED_FIELDS, names)

        content = {name: getattr(self.decision, name) for name in names - IDENTITY_EXCLUDED_FIELDS}
        self.assertEqual(
            decision_identity(content),
            (self.decision.decision_id, self.decision.idempotency_id),
        )

        # Positive control, subset direction: every single omission is visible.
        for dropped in sorted(content):
            with self.subTest(dropped=dropped):
                partial = {key: value for key, value in content.items() if key != dropped}
                self.assertNotEqual(
                    decision_identity(partial)[0], self.decision.decision_id
                )
        # Positive control, superset direction.
        self.assertNotEqual(
            decision_identity({**content, "reviewed_by": ACTOR})[0], self.decision.decision_id
        )

    def test_no_published_request_can_reach_the_decision(self) -> None:
        """The copy-the-Workflow's-hashes path does not exist to be avoided.

        Mutation: give this function a `published: GateRequest` argument and read
        one hash from it. Every static check still passes — the decision is
        well-formed, the ids are derived, the subject was verified moments ago —
        and a human is once again signing a number the Workflow supplied.
        Asserted against the signature because no behavioural test can observe an
        argument that nobody passes.
        """
        signature = inspect.signature(assemble_decision)
        self.assertEqual(
            tuple(signature.parameters), ("derived", "principal", "answer", "issued_at")
        )
        for name, parameter in signature.parameters.items():
            with self.subTest(parameter=name):
                self.assertNotIn("GateRequest", str(parameter.annotation))

        # And behaviourally: a published request disagreeing on every hash exists
        # in scope, is refused by the verifier, and cannot colour the decision.
        published = make_request(
            subject_sha256="1" * 64, admission_sha256="2" * 64, prepared_sha256="3" * 64
        )
        with self.assertRaises(SubmissionRefused):
            verify_published_gate(published, self.derived, now=NOW)
        rebuilt = assemble_decision(self.derived, self.principal, self.answer, NOW)
        self.assertEqual(rebuilt.subject_sha256, self.derived.subject_sha256)
        self.assertEqual(rebuilt.admission_sha256, self.derived.admission_sha256)
        self.assertEqual(rebuilt.prepared_sha256, self.derived.prepared_sha256)
        self.assertEqual(rebuilt, self.decision)

    def test_an_actor_this_gate_does_not_name_is_refused(self) -> None:
        """Mutation: drop the check. Anyone with a shell answers anyone's gate."""
        with self.assertRaises(SubmissionRefused) as raised:
            assemble_decision(self.derived, make_principal("human-alex"), self.answer, NOW)
        self.assertEqual(
            str(raised.exception),
            "This gate may only be answered by 'human-sam', and this process is 'human-alex'",
        )

    def test_a_naive_issued_at_is_refused(self) -> None:
        for value in (NOW.replace(tzinfo=None), NOW.isoformat(), None):
            with self.subTest(issued_at=value), self.assertRaises(SubmissionRefused) as raised:
                assemble_decision(self.derived, self.principal, self.answer, value)
            self.assertEqual(str(raised.exception), "Decision timestamp must be an aware datetime")

    def test_arguments_must_be_exact_types(self) -> None:
        for arguments, message in (
            (
                (dataclasses.asdict(self.derived), self.principal, self.answer, NOW),
                "Derived subject must be an exact DerivedSubject",
            ),
            (
                (self.derived, dataclasses.asdict(self.principal), self.answer, NOW),
                "Principal must be an exact Principal",
            ),
            (
                (self.derived, self.principal, dataclasses.asdict(self.answer), NOW),
                "Answer must be an exact Answer",
            ),
        ):
            with self.subTest(message=message), self.assertRaises(SubmissionRefused) as raised:
                assemble_decision(*arguments)
            self.assertEqual(str(raised.exception), message)


# --------------------------------------------------------------------- journal


class JournalPathTests(TemporaryRootTestCase):
    def test_the_path_names_both_components(self) -> None:
        self.assertEqual(
            journal_path(self.root, WORKFLOW_ID, GATE_ID),
            self.root / f"{WORKFLOW_ID}.{GATE_ID}.json",
        )

    def test_the_root_must_be_a_path(self) -> None:
        with self.assertRaises(SubmissionRefused) as raised:
            journal_path(str(self.root), WORKFLOW_ID, GATE_ID)  # type: ignore[arg-type]
        self.assertEqual(str(raised.exception), "Journal root must be a Path")

    def test_no_component_can_leave_the_journal_root(self) -> None:
        """`ID_PATTERN` already forbids `/`; the check keeps that local and loud."""
        for bad in ("../evil", "a/b", "", "/etc/passwd", ".", "-leading-dash", None):
            with self.subTest(workflow_id=bad), self.assertRaises(SubmissionRefused) as raised:
                journal_path(self.root, bad, GATE_ID)
            self.assertEqual(str(raised.exception), "Invalid journal workflow id")
            with self.subTest(gate_id=bad), self.assertRaises(SubmissionRefused) as raised:
                journal_path(self.root, WORKFLOW_ID, bad)
            self.assertEqual(str(raised.exception), "Invalid journal gate id")


class JournalTests(TemporaryRootTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.journal_root = self.root / "submissions"
        self.decision = make_decision()

    def path(self) -> Path:
        return journal_path(self.journal_root, WORKFLOW_ID, GATE_ID)

    def document(self, **overrides: Any) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": 1,
            "workflow_id": WORKFLOW_ID,
            "gate_id": GATE_ID,
            "subject_sha256": self.decision.subject_sha256,
            "decision": dataclasses.asdict(self.decision),
        }
        document.update(overrides)
        return document

    def write_raw(self, body: str) -> Path:
        self.journal_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path()
        path.write_text(body, encoding="utf-8")
        return path

    def read(self, subject_sha256: str | None = None) -> GateDecision | None:
        return read_journal(
            self.journal_root,
            WORKFLOW_ID,
            GATE_ID,
            self.decision.subject_sha256 if subject_sha256 is None else subject_sha256,
        )

    # -- writing

    def test_writing_creates_a_private_directory_and_a_private_file(self) -> None:
        """The journal holds a signed decision; the mode is the whole protection."""
        self.assertFalse(self.journal_root.exists())
        path = write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        self.assertEqual(path, self.path())
        self.assertEqual(stat.S_IMODE(self.journal_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_written_bytes_are_the_canonical_entry(self) -> None:
        path = write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            canonical_json(
                JournalEntry(
                    1, WORKFLOW_ID, GATE_ID, self.decision.subject_sha256, self.decision
                )
            ),
        )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.document())

    def test_rewriting_truncates_rather_than_overlaying(self) -> None:
        """A shorter second write must not leave the tail of a longer first one.

        Not a hypothetical: the rationale is free text up to 2048 characters, so
        a second answer is very often shorter than the first. Without O_TRUNC the
        file would be valid JSON followed by garbage, and `read_journal` would
        refuse forever with no way for a human to see why.
        """
        verbose = assemble_decision(
            make_derived(), make_principal(), make_answer("approve", "x" * 2048), NOW
        )
        write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, verbose)
        long_size = self.path().stat().st_size
        write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        self.assertLess(self.path().stat().st_size, long_size)
        self.assertEqual(self.read(), self.decision)

    def test_writing_requires_an_exact_decision(self) -> None:
        with self.assertRaises(SubmissionRefused) as raised:
            write_journal(
                self.journal_root, WORKFLOW_ID, GATE_ID, dataclasses.asdict(self.decision)
            )
        self.assertEqual(str(raised.exception), "Journal decision must be an exact GateDecision")

    def test_writing_validates_the_path_components_too(self) -> None:
        with self.assertRaises(SubmissionRefused) as raised:
            write_journal(self.journal_root, "../evil", GATE_ID, self.decision)
        self.assertEqual(str(raised.exception), "Invalid journal workflow id")
        self.assertFalse(self.journal_root.exists(), "nothing is created before validation")

    # -- reading

    def test_a_written_decision_reads_back_exactly(self) -> None:
        write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        recovered = self.read()
        self.assertEqual(recovered, self.decision)
        self.assertEqual(recovered.issued_at, self.decision.issued_at)
        self.assertEqual(recovered.idempotency_id, self.decision.idempotency_id)

    def test_a_missing_journal_is_not_an_error(self) -> None:
        """First answer of the run: there is nothing recorded and that is normal."""
        self.assertIsNone(self.read())
        self.assertFalse(self.journal_root.exists(), "reading creates nothing")

    def test_a_recorded_decision_for_a_different_subject_is_never_reused(self) -> None:
        """THE STALE-APPROVAL DEFENCE. A re-raised gate must not inherit an answer.

        Mutation: return the entry regardless of subject. In production the gate
        is re-raised over changed bytes — a rebuilt APK, a corrected verification
        report — and this client would resubmit yesterday's `approve`, correctly
        signed, correctly journalled, for something the human never saw. That is
        the exact failure the whole hash chain exists to prevent, arriving
        through the client's own cache.

        `None` rather than a refusal is right: nothing is wrong, the human simply
        has not answered *this* subject yet.
        """
        write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        # The file is there, it parses, and it holds a valid decision by the
        # right actor for the right gate. Only the subject moved.
        self.assertTrue(self.path().is_file())
        self.assertEqual(self.read(), self.decision)
        self.assertIsNone(self.read("9" * 64))

    def test_unreadable_json_is_refused_rather_than_ignored(self) -> None:
        """A corrupt journal is not an absent one: it may hold a submitted answer."""
        path = self.write_raw('{"schema_version": 1, "workflo')
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(str(raised.exception), f"Journal entry is not readable JSON: {path}")

    def test_a_journal_that_is_not_an_object_is_refused(self) -> None:
        self.write_raw("[]")
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(str(raised.exception), "journal entry must be an object with string keys")

    def test_an_unknown_field_is_refused(self) -> None:
        self.write_raw(json.dumps(self.document(submitted_at=NOW.isoformat())))
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(str(raised.exception), "Unknown journal entry field: submitted_at")

    def test_a_missing_field_is_refused(self) -> None:
        document = self.document()
        del document["subject_sha256"]
        self.write_raw(json.dumps(document))
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(str(raised.exception), "Missing journal entry field: subject_sha256")

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        for version in (2, 0, "1", None):
            with self.subTest(version=version):
                self.write_raw(json.dumps(self.document(schema_version=version)))
                with self.assertRaises(SubmissionRefused) as raised:
                    self.read()
                self.assertEqual(str(raised.exception), "Unsupported journal entry schema")

    def test_an_invalid_decision_is_refused(self) -> None:
        broken = {**dataclasses.asdict(self.decision), "subject_sha256": "not-a-digest"}
        self.write_raw(json.dumps(self.document(decision=broken)))
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(
            str(raised.exception),
            "Journal entry holds an invalid decision: Invalid decision subject SHA-256",
        )

    def test_an_entry_naming_a_different_gate_is_refused(self) -> None:
        """The filename is not the authority; the entry has to say so itself."""
        for overrides in (
            {"workflow_id": "port-440-replay-workflow"},
            {"gate_id": "port-440-replay-final-verification-gate"},
        ):
            with self.subTest(**overrides):
                path = self.write_raw(json.dumps(self.document(**overrides)))
                with self.assertRaises(SubmissionRefused) as raised:
                    self.read()
                self.assertEqual(
                    str(raised.exception), f"Journal entry names a different gate: {path}"
                )

    def test_an_entry_whose_decision_does_not_bind_its_subject_is_refused(self) -> None:
        """The envelope agreeing with the request is not the decision agreeing.

        A hand-edited entry whose envelope says the right subject while the
        signed decision says another would otherwise be resubmitted verbatim —
        an answer to a different question, wearing the right label.
        """
        other = assemble_decision(
            make_derived(subject_sha256="9" * 64), make_principal(), make_answer(), NOW
        )
        path = self.write_raw(json.dumps(self.document(decision=dataclasses.asdict(other))))
        with self.assertRaises(SubmissionRefused) as raised:
            self.read()
        self.assertEqual(
            str(raised.exception), f"Journal entry decision does not bind its subject: {path}"
        )


# ------------------------------------------------------------------ gate kinds


class GateKindTests(unittest.TestCase):
    def test_the_replay_gate_is_recognised_by_its_run_scoped_id(self) -> None:
        self.assertIs(select_gate_kind(GATE_ID, RUN_ID), REPLAY_VERIFICATION_GATE)
        self.assertEqual(REPLAY_VERIFICATION_GATE.name, "replay-final-verification")
        self.assertEqual(REPLAY_VERIFICATION_GATE.update_name, "submit_verification_decision")

    def test_a_gate_minted_for_another_run_does_not_match(self) -> None:
        """Run scoping is the whole matcher: two runs' gates share a suffix.

        A prefix or suffix test would let a decision derived for one run be
        submitted against another's identically-named gate.
        """
        other_run = "port-440-replay"
        self.assertNotEqual(other_run, RUN_ID)
        self.assertFalse(REPLAY_VERIFICATION_GATE.matches(GATE_ID, other_run))
        with self.assertRaises(SubmissionRefused):
            select_gate_kind(GATE_ID, other_run)
        # Positive control: the matcher is not simply always false.
        self.assertTrue(
            REPLAY_VERIFICATION_GATE.matches(f"{other_run}-final-verification-gate", other_run)
        )
        self.assertTrue(REPLAY_VERIFICATION_GATE.matches(GATE_ID, RUN_ID))

    def test_the_suffix_is_matched_whole(self) -> None:
        for gate_id in (
            f"{RUN_ID}-final-verification-gate-2",
            f"x{RUN_ID}-final-verification-gate",
            f"{RUN_ID}-final-verification",
            RUN_ID,
        ):
            with self.subTest(gate_id=gate_id):
                self.assertFalse(REPLAY_VERIFICATION_GATE.matches(gate_id, RUN_ID))

    def test_phase_a_approval_is_refused_rather_than_answered(self) -> None:
        """Refusing to answer is the correct behaviour and the honest one.

        `PortRunWorkflow`'s subject binds operation outputs the ledger indexes by
        content rather than by run, so a client holding a run id cannot reach
        them. Falling back to the published hashes would be a human signing a
        number nobody reproduced — the one thing this module exists to prevent.
        """
        with self.assertRaises(SubmissionRefused) as raised:
            select_gate_kind("phase-a-approval", RUN_ID)
        self.assertEqual(
            str(raised.exception),
            "No resolver is registered for gate 'phase-a-approval'. This client will "
            "not submit a decision whose subject it cannot independently reproduce.",
        )

    def test_an_unknown_gate_is_refused(self) -> None:
        with self.assertRaises(SubmissionRefused) as raised:
            select_gate_kind("some-other-gate", RUN_ID)
        self.assertEqual(
            str(raised.exception),
            "No resolver is registered for gate 'some-other-gate'. This client will "
            "not submit a decision whose subject it cannot independently reproduce.",
        )

    def test_only_the_reproducible_gate_is_registered(self) -> None:
        """Registering a second kind is a decision, not an import side effect."""
        self.assertEqual(GATE_KINDS, (REPLAY_VERIFICATION_GATE,))


# ---------------------------------------------------------------- confirmation


class CheckConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pending = make_pending()
        self.prefix = SUBJECT_SHA[:CONFIRMATION_LENGTH]

    def test_twelve_characters_of_the_derived_hash_are_enough(self) -> None:
        self.assertEqual(CONFIRMATION_LENGTH, 12)
        self.assertEqual(len(self.prefix), 12)
        for confirmation in (self.prefix, SUBJECT_SHA[:20], SUBJECT_SHA):
            with self.subTest(confirmation=confirmation):
                self.assertIsNone(check_confirmation(self.pending, confirmation))

    def test_case_and_surrounding_whitespace_are_forgiven(self) -> None:
        """A hash pasted out of a terminal arrives with a newline on it.

        Folding case and stripping whitespace costs nothing here — the subject is
        already verified, and this check is evidence a human read it, not a
        secret. Refusing a trailing newline would only teach people to stop
        pasting and start retyping, which is worse.
        """
        for confirmation in (
            self.prefix.upper(),
            f"  {self.prefix}  ",
            f"\t{self.prefix.upper()}\n",
            SUBJECT_SHA.upper(),
        ):
            with self.subTest(confirmation=confirmation):
                self.assertIsNone(check_confirmation(self.pending, confirmation))

    def test_eleven_characters_are_not_enough(self) -> None:
        """Mutation: lower the length. Then "" confirms whatever was pending."""
        for confirmation in (SUBJECT_SHA[:11], "", " ", SUBJECT_SHA[:11].upper()):
            with self.subTest(confirmation=confirmation), self.assertRaises(
                SubmissionRefused
            ) as raised:
                check_confirmation(self.pending, confirmation)
            self.assertEqual(
                str(raised.exception),
                "Confirmation must be at least 12 characters of the derived subject hash",
            )

    def test_a_prefix_of_a_different_hash_is_refused(self) -> None:
        for confirmation in ("abcdef012346", "0" * 12, ADMISSION_SHA[:12], SUBJECT_SHA[1:13]):
            with self.subTest(confirmation=confirmation), self.assertRaises(
                SubmissionRefused
            ) as raised:
                check_confirmation(self.pending, confirmation)
            self.assertEqual(
                str(raised.exception),
                f"Confirmation does not match the derived subject hash {SUBJECT_SHA}",
            )

    def test_the_hash_quoted_is_the_derived_one_not_the_published_one(self) -> None:
        """What a human confirms has to be what the client vouched for."""
        pending = PendingGate(
            WORKFLOW_ID,
            REPLAY_VERIFICATION_GATE,
            make_request(subject_sha256="7" * 64),
            make_derived(),
        )
        self.assertIsNone(check_confirmation(pending, self.prefix))
        with self.assertRaises(SubmissionRefused):
            check_confirmation(pending, "7" * 12)

    def test_a_non_string_confirmation_is_refused(self) -> None:
        for confirmation in (None, 123456789012, b"abcdef012345", ["abcdef012345"]):
            with self.subTest(confirmation=confirmation), self.assertRaises(
                SubmissionRefused
            ) as raised:
                check_confirmation(self.pending, confirmation)
            self.assertEqual(str(raised.exception), "Confirmation must be a string")


# -------------------------------------------------------------------- describe


class DescribeTests(unittest.TestCase):
    def test_the_rendering_shows_the_derived_hash_and_the_confirmation_prefix(self) -> None:
        rendered = describe(make_pending())
        self.assertIn(SUBJECT_SHA, rendered)
        self.assertIn(f"--confirm {SUBJECT_SHA[:CONFIRMATION_LENGTH]}", rendered)
        # The prefix it prints is one a human can actually pass back.
        instruction = [line for line in rendered.splitlines() if "--confirm" in line][0]
        check_confirmation(make_pending(), instruction.split("--confirm ")[1])

    def test_the_hash_shown_is_the_derived_one_not_the_published_one(self) -> None:
        """What a human reads must be what the client vouched for.

        Found by mutation: swapping `derived` for `pending.published` here left
        the suite green, because every other fixture agrees on the subject hash
        — which is precisely the state `read_pending_gate` guarantees before a
        `PendingGate` exists. That makes the two spellings indistinguishable in
        production and the intent worth pinning anyway: `describe` is also the
        whole output of the `show` subcommand, where the number a human reads is
        the only thing they get.
        """
        pending = PendingGate(
            WORKFLOW_ID,
            REPLAY_VERIFICATION_GATE,
            make_request(subject_sha256="7" * 64),
            make_derived(),
        )
        rendered = describe(pending)
        self.assertIn(SUBJECT_SHA, rendered)
        self.assertIn(f"--confirm {SUBJECT_SHA[:CONFIRMATION_LENGTH]}", rendered)
        self.assertNotIn("7" * 64, rendered)
        self.assertNotIn("7" * CONFIRMATION_LENGTH, rendered)

    def test_the_rendering_names_the_gate_the_run_and_who_may_answer(self) -> None:
        rendered = describe(make_pending())
        for value in (WORKFLOW_ID, GATE_ID, RUN_ID, POLICY, ACTOR, ISSUED_AT, EXPIRES_AT):
            with self.subTest(value=value):
                self.assertIn(value, rendered)
        self.assertIn(REPLAY_VERIFICATION_GATE.name, rendered)


class MainExitCodeTests(TemporaryRootTestCase):
    """The CLI's exit codes are a contract; an operator's scripts read them.

    Only the refusals that happen *before* a connection are exercised here — the
    rest need a server and live in `test_submission_temporal`. What matters is
    that an ordinary operator mistake produces a sentence and exit 2, not a
    traceback: a client that shows tracebacks for wrong paths teaches whoever is
    answering a gate to skim tracebacks, which is the last habit they should
    have.
    """

    def run_main(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io

        from dfinsta_pipeline.submission import main

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(list(argv))
        code = raised.exception.code
        return (code if isinstance(code, int) else 1), stderr.getvalue()

    def test_a_missing_principal_refuses_with_exit_2(self) -> None:
        code, message = self.run_main(
            "--state-root", str(self.root),
            "--principal", str(self.root / "absent.json"),
            "show", WORKFLOW_ID,
        )
        self.assertEqual(code, 2)
        self.assertIn("refused:", message)
        self.assertIn("No principal file", message)
        # The message has to be actionable, or it is just a different traceback.
        self.assertIn("mode 0600", message)

    def test_a_state_root_with_no_ledger_refuses_with_exit_2(self) -> None:
        principal = self.root / "principal.json"
        principal.write_text(
            json.dumps({"schema_version": 1, "uid": os.geteuid(), "actor": ACTOR}),
            encoding="utf-8",
        )
        principal.chmod(0o600)
        empty = self.root / "empty-state"
        empty.mkdir()

        code, message = self.run_main(
            "--state-root", str(empty),
            "--principal", str(principal),
            "show", WORKFLOW_ID,
        )
        self.assertEqual(code, 2)
        self.assertIn("No ledger under --state-root", message)
        # And it did not quietly create one on the way past.
        self.assertFalse((empty / "ledger.sqlite3").exists())

    def test_an_unknown_verdict_is_rejected_by_the_parser(self) -> None:
        code, message = self.run_main(
            "--state-root", str(self.root),
            "submit", WORKFLOW_ID,
            "--verdict", "maybe", "--rationale", "x", "--confirm", "a" * 12,
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", message)


if __name__ == "__main__":
    unittest.main()

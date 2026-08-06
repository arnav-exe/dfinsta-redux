"""Attribution: when a claim was recorded, which version it is about, which APK.

Until 2026-08-06 an `EvidenceClaim` carried `hook_id` and nothing else that
identified the port. The version was knowable only from the *filename* somebody
chose (`manifest/runtime_evidence/440.jsonl`) — in the path, not the data. No
claim named a build: a `runtime_probe` recorded a device serial and never which
APK was installed on it, so 440's device evidence cannot be joined to the
artifact it measured. And `recorded_at` is `""` on all thirty committed claims,
because `stamped()` existed and the driver never called it. A report could not
order claims in time, date a port, or join a claim to the thing it is about.

Three fields fix that, and the whole risk of adding them is in one place:

**A claim's identity is the bytes of its dict.** `claim_id` is
`canonical_sha256(to_dict())` and `supersedes` names a parent by that id. An
additive field that emitted `"version": null` on every claim would re-hash every
claim already on disk and break every stored supersede chain — silently, since
nothing recomputes an id and compares it to the file. So `to_dict` omits both new
keys when they are unset, and :class:`CommittedLedgerTests` is that sentence
written as a test. It is the one class here worth keeping if every other
assertion in this file were deleted.

The rest follows the shape of the three fields:

**A pre-apply claim may not name a build, and the rule has two halves that
disagree on purpose.** `EvidenceClaim.__post_init__` *refuses* one — a hash there
would be a claim about an artifact that did not exist when the fact was
established. :func:`attributed` *drops* it — a caller attributing a whole run has
one build hash and a mix of phases, and making each call site classify its own
claim duplicates `PHASES` in n places, which is how two copies of a rule drift.
:class:`PreApplyBuildHashTests` drives both halves and, in one test, both at once
on the same input: the strict path and the safe path are the split, and a change
that made them agree would delete the check.

**The ledger is where a run can forget.** A claim reaches the file through
exactly one method, so `EvidenceLedger(path, attribution=...)` applies the
attribution in `record` — one place, rather than one per builder.
:class:`LedgerAttributionTests`. The exception is a claim that arrives already
naming a version, which keeps its own; see :class:`KnownGapTests` for what that
escape hatch is for and why nothing currently reaches it.

**The build hash arrives late.** The APK does not exist when the ledger is
opened, so `bind_build` is separate from the constructor, claims written before
it keep no hash, and re-binding a *different* hash is refused: one run, one
artifact, and a ledger whose later claims pointed at a second one would be the
worst kind of wrong — every claim individually true, the set describing no
artifact that ever existed. :class:`BindBuildTests`.

`PortAttributionTests` is the only class that proves any of this happens in a
run. Everything above it tests a library, and a library that nothing calls would
leave the ledger exactly as unjoinable as it was, which is indistinguishable from
the state before the change for anyone reading one.

**Mutation results.** Each was applied alone to a fresh whole-repo copy, with the
unmutated copy passing first as the control:

* `to_dict` always emitting both keys, as null → 13 tests across six classes,
  five of them in :class:`CommittedLedgerTests` — the round-trip, the line, the
  id, the omission and the control
* dropping the pre-apply guard from `EvidenceClaim.__post_init__` →
  :meth:`PreApplyBuildHashTests.
  test_the_constructor_refuses_a_build_hash_on_every_pre_apply_kind` and
  :meth:`PreApplyBuildHashTests.
  test_the_two_halves_disagree_on_the_same_input_and_that_is_the_design`
* `attributed` passing `build_sha256` through unconditionally → 7 tests in four
  classes. Note what does *not* fail: no `PortAttributionTests` test does,
  because the run binds its hash only after the last pre-apply claim is already
  written. The driver is protected by the order it happens to do things in, and
  the rule is what protects the next caller.
* `bind_build` overwriting instead of refusing a different hash →
  :class:`BindBuildTests`, both the refusal and the state left behind by it
* `record()` overwriting an already-set `version` → the two
  :class:`LedgerAttributionTests` tests about a claim that brought its own

`KnownGapTests` and `PortKnownGapTests` record three behaviours found while
writing this and reported rather than fixed, so a later fix fails loudly instead
of quietly changing what the ledger says. The third is the one worth reading: a
verifier report whose `apk_sha256` is spelled in uppercase passes the driver's
length-only guard, fails the claim's lowercase-hex check, and takes down a run
whose APK is already built and verified.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Mapping

from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.driver import DriverError
from dfinsta_pipeline.evidence import (
    PHASES,
    POST_BUILD,
    PRE_APPLY,
    Attribution,
    EvidenceClaim,
    EvidenceError,
    EvidenceKind,
    EvidenceLedger,
    Subject,
    attributed,
)
from tests.test_driver import (
    ACTION_BAR_HOOK,
    CONTEXT_HOOK,
    ENDPOINT_HOOK,
    STAMP,
    DriverCase,
)
from tests.test_evidence import as_bytes, claim_for

REPO = Path(__file__).resolve().parents[1]

#: The committed baselines. Not a fixture and not a copy: these are the files the
#: differential reads, and the ones a change to `to_dict` would silently orphan.
COMMITTED = REPO / "manifest" / "runtime_evidence"

#: The two that existed when this file was written. Every `*.jsonl` in the
#: directory is checked, so a later version's baseline is covered the moment it
#: lands; these two are named so an empty or renamed directory fails loudly
#: instead of making every loop below pass over nothing.
KNOWN_LEDGERS = ("439.jsonl", "440.jsonl")

#: 7 on 439 and 23 on 440. A floor rather than an equality, because a baseline
#: accumulates — the README asks each port to record every shape it can — and a
#: test that forbade that would be read as forbidding it.
COMMITTED_CLAIMS = 30

#: Every key a claim carried before the change. The set is written out rather
#: than derived from `to_dict`, so a field added to both at once still fails.
LEGACY_KEYS = frozenset(
    {
        "schema_version",
        "hook_id",
        "kind",
        "verdict",
        "producer",
        "actor",
        "summary",
        "detail",
        "confidence",
        "decision_id",
        "rationale",
        "supersedes",
        "recorded_at",
    }
)

#: A valid 64-character lowercase hex digest, and a second one for the re-bind
#: refusal. Deliberately not `"a" * 64`: a hash whose characters are all the same
#: cannot tell a truncation from a substitution.
BUILD = "3f" + "a1b2c3d4e5" * 6 + "9d"
OTHER_BUILD = "7c" + "0f1e2d3c4b" * 6 + "5a"

#: The clock, from the caller, for the unit tests. `STAMP` (imported) is
#: `test_driver`'s and is what the driver tests pass to `port`.
WHEN = "2026-08-06T09:15:00+00:00"

HOOK = "tigon_url_block"

PRE_APPLY_KINDS = tuple(
    kind for kind in EvidenceKind if PHASES[kind] == PRE_APPLY
)
POST_BUILD_KINDS = tuple(
    kind for kind in EvidenceKind if PHASES[kind] == POST_BUILD
)


def committed_ledgers() -> list[Path]:
    """Every baseline JSONL on disk, with the two known ones proven present."""
    found = sorted(COMMITTED.glob("*.jsonl"))
    names = {path.name for path in found}
    assert set(KNOWN_LEDGERS) <= names, f"{COMMITTED} lost a baseline: {sorted(names)}"
    return found


def committed_rows() -> list[tuple[Path, int, str, dict[str, Any]]]:
    """`(file, line number, the exact line, the parsed dict)` for every claim."""
    rows: list[tuple[Path, int, str, dict[str, Any]]] = []
    for path in committed_ledgers():
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                rows.append((path, number, line, json.loads(line)))
    return rows


def dumped(data: Mapping[str, Any]) -> str:
    """One claim in the form `record_runtime.append_claims` writes.

    Not `canonical_json`: the committed files were written by that function, and
    the point of the byte comparison below is to reproduce the file rather than
    to agree with a second serialiser.
    """
    return json.dumps(data, sort_keys=True)


# ------------------------------------------------------- the claims already on disk


class CommittedLedgerTests(unittest.TestCase):
    """Thirty claims are on disk. None of them may move.

    `claim_id` is a content hash of `to_dict()` and `supersedes` names a parent
    by it, so adding a field that always serialises re-identifies every claim
    ever written. Nothing else in the tree would notice: no reader recomputes an
    id and checks it against the file, and a broken supersede chain looks exactly
    like a claim that never had a parent.
    """

    def test_the_committed_corpus_is_not_empty(self):
        """The positive control for every loop in this class.

        Each test below iterates the baselines and asserts per claim. A glob that
        matched nothing — a renamed directory, a moved manifest, a test run from
        the wrong root — would make all of them pass while checking nothing, and
        that is the failure mode a suite never announces. An absence assertion
        needs a way to fail.
        """
        rows = committed_rows()

        self.assertGreaterEqual(len(rows), COMMITTED_CLAIMS)
        for name in KNOWN_LEDGERS:
            with self.subTest(ledger=name):
                self.assertTrue(
                    any(path.name == name for path, _, _, _ in rows),
                    f"{name} contributed no claims",
                )

    def test_every_committed_claim_round_trips_to_the_same_dict(self):
        """`from_dict` then `to_dict` must give back what was read, byte for byte.

        Compared as canonical bytes rather than as dicts because the failure this
        is aimed at is a *type* change as much as a key change: `"confidence":
        null` becoming `0`, `True` becoming `1`. Dict equality catches an added
        key; it does not catch a value that serialises differently, and the id is
        a hash of the serialisation.
        """
        for path, number, _, data in committed_rows():
            with self.subTest(ledger=path.name, line=number):
                claim = EvidenceClaim.from_dict(data)
                self.assertEqual(as_bytes(claim.to_dict()), as_bytes(data))

    def test_every_committed_claim_keeps_its_id(self):
        """The invariant a supersede chain rests on, stated directly.

        The id is not stored in the file — it is derived from it — so the only
        way to check that a claim's identity survived a schema change is to hash
        what is on disk and hash what the code produces from it. If these two ever
        differ, every `supersedes` value written before the change points at a
        claim that no longer exists under that name.
        """
        for path, number, _, data in committed_rows():
            with self.subTest(ledger=path.name, line=number):
                self.assertEqual(
                    EvidenceClaim.from_dict(data).claim_id, canonical_sha256(data)
                )

    def test_every_committed_line_is_reproduced_exactly(self):
        """The file itself, not just the dict parsed out of it.

        `record_runtime.append_claims` writes `json.dumps(claim.to_dict(),
        sort_keys=True)`, so a claim read back and re-written must reproduce the
        line it came from. This is the assertion that would catch a change nobody
        thought of — a key renamed, a default that stopped being a default — as a
        diff in a committed file rather than as a subtle change in an id.
        """
        for path, number, line, data in committed_rows():
            with self.subTest(ledger=path.name, line=number):
                claim = EvidenceClaim.from_dict(data)
                self.assertEqual(dumped(claim.to_dict()) + "\n", line)

    def test_no_committed_claim_carries_a_version_or_a_build_hash(self):
        """The control that makes the three tests above mean what they say.

        If the baselines had been rewritten with attribution, every round-trip
        above would still pass — and would be proving that attributed claims
        round-trip, not that adding the fields left the old ones alone. These
        thirty are pre-change data and must stay that way to be worth testing.
        """
        for path, number, _, data in committed_rows():
            with self.subTest(ledger=path.name, line=number):
                self.assertNotIn("version", data)
                self.assertNotIn("build_sha256", data)
                self.assertEqual(set(data) - LEGACY_KEYS, set())

    def test_an_unattributed_claim_omits_both_keys_rather_than_recording_null(self):
        """Absent, not null. The distinction IS the compatibility guarantee.

        `"version": null` and no `version` key are the same information to a
        reader and different bytes to a hash. Only one of them leaves the thirty
        claims on disk with the ids they were written under. This is also the
        rule the gate journal follows for `payload_sha256`, for the same reason.
        """
        claim = claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)
        data = claim.to_dict()

        self.assertNotIn("version", data)
        self.assertNotIn("build_sha256", data)
        self.assertIsNone(claim.version)
        self.assertIsNone(claim.build_sha256)
        self.assertEqual(set(data), LEGACY_KEYS)

    def test_an_attributed_claim_does_carry_both_keys(self):
        """Positive control for the omission above.

        Omitting a key is only meaningful if the key can appear. A `to_dict` that
        dropped `version` unconditionally would satisfy every assertion in this
        class and lose the field entirely.
        """
        data = attributed(
            claim_for(HOOK, EvidenceKind.RUNTIME_PROBE),
            recorded_at=WHEN,
            version="440",
            build_sha256=BUILD,
        ).to_dict()

        self.assertEqual(data["version"], "440")
        self.assertEqual(data["build_sha256"], BUILD)
        self.assertEqual(data["recorded_at"], WHEN)
        self.assertEqual(set(data), LEGACY_KEYS | {"version", "build_sha256"})

    def test_attributing_a_committed_claim_would_change_its_id(self):
        """Why the omission matters, measured on the real data.

        The three round-trip tests are about the fields staying out of the bytes.
        This is the other half: the fields are in the hash when they are set, so
        emitting them as null on every claim really would have re-identified all
        thirty. Without this the class could pass under a `to_dict` that ignored
        the new fields altogether, and would be pinning nothing.
        """
        _, _, _, data = committed_rows()[0]
        claim = EvidenceClaim.from_dict(data)

        moved = attributed(claim, recorded_at=WHEN, version="440", build_sha256=BUILD)
        self.assertNotEqual(moved.claim_id, claim.claim_id)
        # And the file is still what it was: attribution returns a new claim.
        self.assertEqual(as_bytes(claim.to_dict()), as_bytes(data))


# --------------------------------------------------------------- identity moves


class AttributedIdentityTests(unittest.TestCase):
    """An attributed claim is a different claim, and it survives the file."""

    def test_attribution_changes_the_claim_id(self):
        """A claim that says which port it is about is not the same fact.

        Recorded here as a property rather than a caveat: two claims that differ
        only in attribution have different ids, so a supersede chain built across
        the change names one or the other and never both.
        """
        claim = claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)

        self.assertNotEqual(
            attributed(claim, recorded_at=WHEN, version="440").claim_id, claim.claim_id
        )

    def test_each_field_moves_the_id_on_its_own(self):
        """Three fields, three distinct ids — none of them is decorative.

        A `to_dict` that emitted `version` but forgot `build_sha256` would leave
        two claims about two different APKs sharing one id, which is the case
        where a content hash actively misleads: the ledger would report a probe
        against build A as already recorded when it was run against build B.
        """
        claim = claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)
        ids = {
            "bare": claim.claim_id,
            "stamped": replace(claim, recorded_at=WHEN).claim_id,
            "versioned": replace(claim, version="440").claim_id,
            "built": replace(claim, build_sha256=BUILD).claim_id,
        }

        self.assertEqual(len(set(ids.values())), len(ids), ids)

    def test_an_attributed_claim_round_trips_through_from_dict(self):
        """Through JSON, because that is the only way the field ever travels.

        `EvidenceLedger.load` and `differential.read_claims` both rebuild every
        claim with `from_dict`. A field written by `to_dict` and not read by
        `from_dict` is a field that exists until the next run reads the file, and
        the loss would be invisible — the claim still validates, it just quietly
        stops naming its port.
        """
        for kind in EvidenceKind:
            with self.subTest(kind=kind.value):
                claim = attributed(
                    claim_for(HOOK, kind),
                    recorded_at=WHEN,
                    version="440",
                    build_sha256=BUILD,
                )
                restored = EvidenceClaim.from_dict(json.loads(json.dumps(claim.to_dict())))

                self.assertEqual(restored, claim)
                self.assertEqual(restored.claim_id, claim.claim_id)
                self.assertEqual(restored.version, "440")

    def test_a_claim_read_back_from_an_old_file_has_no_version(self):
        """`from_dict` must not invent one for a dict that has no key.

        `data.get("version")` returning None is what lets a pre-change file load
        at all. A default of `""` here would be refused by the constructor as a
        blank version and make every committed baseline unreadable — the exact
        opposite failure, and just as total.
        """
        _, _, _, data = committed_rows()[0]
        claim = EvidenceClaim.from_dict(data)

        self.assertIsNone(claim.version)
        self.assertIsNone(claim.build_sha256)


# ------------------------------------------------- the pre-apply rule, both halves


class PreApplyBuildHashTests(unittest.TestCase):
    """A claim established before the build may not name one — refused, or dropped.

    Absence of a build hash means "this claim predates the artifact", not
    "unknown". That reading only holds if a pre-apply claim can never carry one,
    which is enforced twice and differently on purpose: the constructor refuses,
    so a claim built by hand cannot lie; `attributed` drops, so a caller with one
    hash and a batch of mixed phases does not have to re-implement `PHASES`.
    """

    def test_the_phase_table_covers_every_kind_and_splits_four_from_three(self):
        """The ground truth both halves consult, pinned before either is tested.

        `PHASES[claim.kind]` is a bare subscript in two places; a kind missing
        from the table is a `KeyError` out of a constructor. And "the pre-apply
        kinds" is a claim about which four, made in the docstrings of three
        functions — if the membership changed, every test below would keep
        passing while asserting about a different set.
        """
        self.assertEqual(set(PHASES), set(EvidenceKind))
        self.assertEqual(
            sorted(kind.value for kind in PRE_APPLY_KINDS),
            [
                "adversarial_verified",
                "anchor_unique",
                "proposer_agreement",
                "registers_safe",
            ],
        )
        self.assertEqual(
            sorted(kind.value for kind in POST_BUILD_KINDS),
            ["differential", "runtime_probe", "static_verified"],
        )

    def test_the_constructor_refuses_a_build_hash_on_every_pre_apply_kind(self):
        """Built by hand, refused. All four kinds, not just a representative one.

        This is the strict half. A hash on an `anchor_unique` claim is an
        assertion about an artifact that did not exist when the anchor was
        checked — the fact is true of a decode, and pinning it to an APK invites a
        reader to conclude the hook was proven in that build.
        """
        for kind in PRE_APPLY_KINDS:
            with self.subTest(kind=kind.value):
                with self.assertRaises(EvidenceError) as caught:
                    replace(claim_for(HOOK, kind), build_sha256=BUILD)
                self.assertIn("a pre-apply claim cannot name a build", str(caught.exception))
                self.assertIn(kind.value, str(caught.exception))

    def test_the_constructor_accepts_a_build_hash_on_every_post_build_kind(self):
        """Positive control: the refusal is about the phase, not about the field.

        Without this, a constructor that rejected every `build_sha256` outright
        would pass the test above — and the field would be unusable by the three
        kinds it was added for.
        """
        for kind in POST_BUILD_KINDS:
            with self.subTest(kind=kind.value):
                claim = replace(claim_for(HOOK, kind), build_sha256=BUILD)
                self.assertEqual(claim.build_sha256, BUILD)

    def test_attributed_drops_the_build_hash_for_every_pre_apply_kind(self):
        """The safe half: handed a hash, it declines to use it. Silently, and rightly.

        The caller is a run attributing everything it recorded, and it has exactly
        one build. Raising here would force every call site to classify its own
        claim by phase — a second copy of `PHASES`, in n places, each free to
        drift. Dropping keeps the classification in the one table that owns it.
        """
        for kind in PRE_APPLY_KINDS:
            with self.subTest(kind=kind.value):
                claim = attributed(
                    claim_for(HOOK, kind),
                    recorded_at=WHEN,
                    version="440",
                    build_sha256=BUILD,
                )
                self.assertIsNone(claim.build_sha256)
                self.assertNotIn("build_sha256", claim.to_dict())

    def test_attributed_keeps_the_build_hash_for_every_post_build_kind(self):
        """Positive control: dropping is conditional, not the whole behaviour.

        `build_sha256=None` unconditionally would satisfy the test above and quietly
        undo the entire point of the field — a `runtime_probe` would go on naming a
        device serial and no APK, which is exactly 440's unjoinable device evidence.
        """
        for kind in POST_BUILD_KINDS:
            with self.subTest(kind=kind.value):
                claim = attributed(
                    claim_for(HOOK, kind),
                    recorded_at=WHEN,
                    version="440",
                    build_sha256=BUILD,
                )
                self.assertEqual(claim.build_sha256, BUILD)
                self.assertEqual(claim.to_dict()["build_sha256"], BUILD)

    def test_the_two_halves_disagree_on_the_same_input_and_that_is_the_design(self):
        """One kind, one hash: the helper accepts and the constructor refuses.

        Written as a single test because the split is the thing, and the obvious
        "cleanup" is to make them agree. Relaxing the constructor to match the
        helper removes the check entirely; tightening the helper to match the
        constructor pushes phase classification back out to every call site. Each
        direction looks like a simplification and neither is.
        """
        claim = claim_for(HOOK, EvidenceKind.ANCHOR_UNIQUE)

        with self.assertRaises(EvidenceError):
            replace(claim, build_sha256=BUILD)
        self.assertIsNone(
            attributed(claim, recorded_at=WHEN, version="440", build_sha256=BUILD).build_sha256
        )

    def test_a_dropped_hash_does_not_cost_the_version_or_the_timestamp(self):
        """Only the hash is dropped. The other two are the point of the call.

        A pre-apply claim is exactly as much *about* version 440 as a probe is;
        what it is not about is an APK. An implementation that bailed out of
        attribution entirely for pre-apply kinds would leave the four kinds a
        mechanical port produces most of with no version at all.
        """
        claim = attributed(
            claim_for(HOOK, EvidenceKind.REGISTERS_SAFE),
            recorded_at=WHEN,
            version="440",
            build_sha256=BUILD,
        )

        self.assertEqual(claim.version, "440")
        self.assertEqual(claim.recorded_at, WHEN)
        self.assertIsNone(claim.build_sha256)

    def test_attributed_with_no_build_hash_is_the_ordinary_pre_build_call(self):
        """The run's first half: attribution exists, the APK does not yet.

        `build_sha256` defaults to None so that a ledger opened before the build
        can attribute everything it records without inventing a hash, which is
        what `bind_build` exists to supply later.
        """
        claim = attributed(
            claim_for(HOOK, EvidenceKind.STATIC_VERIFIED), recorded_at=WHEN, version="440"
        )

        self.assertIsNone(claim.build_sha256)
        self.assertNotIn("build_sha256", claim.to_dict())
        self.assertEqual(claim.version, "440")


# ------------------------------------------------------------------- validation


class ClaimValidationTests(unittest.TestCase):
    """What the two new fields will and will not accept, and why each rule exists."""

    def test_a_blank_version_is_refused(self):
        """Absent says "not attributed"; empty says "attributed to nothing".

        The second is a lie a reader cannot detect. A row whose version is `""`
        joins to no port and reads, in a table of ports, as a claim someone
        deliberately filed against none of them.
        """
        for blank in ("", " ", "\t", "\n  "):
            with self.subTest(version=repr(blank)):
                with self.assertRaises(EvidenceError) as caught:
                    replace(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE), version=blank)
                self.assertIn("version is present and blank", str(caught.exception))

    def test_an_absent_version_is_accepted(self):
        """Positive control, and the whole compatibility story in one line.

        Every claim on disk has no version. If None were refused the thirty
        committed claims would be unloadable, and `EvidenceLedger.load` would
        raise on the baseline the differential reads.
        """
        claim = replace(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE), version=None)
        self.assertIsNone(claim.version)

    def test_a_build_hash_must_be_sixty_four_lowercase_hex(self):
        """Digests are joined by string equality, so one spelling or none.

        Uppercase is the case that matters: `sha256sum` and Python agree on
        lowercase, and a report that arrived in the other case would produce a
        ledger where two spellings of one artifact never match, so a probe and the
        build it ran against would look like different APKs.
        """
        bad = {
            "uppercase": BUILD.upper(),
            "mixed case": BUILD[:-1] + BUILD[-1].upper(),
            "one short": BUILD[:-1],
            "one long": BUILD + "0",
            "not hex": "g" * 64,
            "empty": "",
            "whitespace": " " * 64,
            "trailing newline": BUILD[:-1] + "\n",
        }
        for label, value in bad.items():
            with self.subTest(build_sha256=label):
                with self.assertRaises(EvidenceError) as caught:
                    replace(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE), build_sha256=value)
                self.assertIn("must be a lowercase", str(caught.exception))

    def test_a_valid_hash_and_an_absent_one_are_both_accepted(self):
        """Positive control for the refusals above.

        A validator that rejected everything would pass every assertion in the
        previous test. None is the ordinary state of a pre-apply claim and of every
        claim written before this field existed.
        """
        probe = claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)

        self.assertEqual(replace(probe, build_sha256=BUILD).build_sha256, BUILD)
        self.assertIsNone(replace(probe, build_sha256=None).build_sha256)

    def test_a_refusal_names_the_hook_and_the_kind(self):
        """Messages are read by a human mid-run with seven hooks in flight.

        "build_sha256 must be a lowercase SHA-256" without a hook id is a message
        that identifies the rule and not the claim, and the reader's next question
        — which one? — is the one thing the exception knows and the log does not.
        """
        with self.assertRaises(EvidenceError) as caught:
            replace(
                claim_for("replace_reels_stream_endpoint", EvidenceKind.RUNTIME_PROBE),
                build_sha256="nope",
            )

        self.assertIn("replace_reels_stream_endpoint", str(caught.exception))
        self.assertIn("runtime_probe", str(caught.exception))


# ------------------------------------------------------------- the attribution value


class AttributionValueTests(unittest.TestCase):
    """The three-field value a run holds, and the two things it refuses."""

    def test_an_attribution_needs_a_recorded_at(self):
        """A run that stamps every claim with nothing has attributed nothing.

        `recorded_at` was `""` on all thirty committed claims for precisely this
        reason — the field existed and nobody filled it. Accepting a blank one
        here would rebuild that state one layer up, and this time it would look
        deliberate.
        """
        for blank in ("", "   ", "\n"):
            with self.subTest(recorded_at=repr(blank)):
                with self.assertRaises(EvidenceError) as caught:
                    Attribution(blank, "440")
                self.assertIn("needs a recorded_at", str(caught.exception))

    def test_an_attribution_needs_a_version(self):
        """The same rule the claim enforces, enforced before a claim exists.

        The ledger builds one of these per run and applies it to everything. A
        blank version here would be applied to every claim of the run and refused
        by each of them, so the run would fail at the first `record` — which is
        the right outcome reported at the wrong place.
        """
        for blank in ("", "   ", "\t"):
            with self.subTest(version=repr(blank)):
                with self.assertRaises(EvidenceError) as caught:
                    Attribution(WHEN, blank)
                self.assertIn("needs a version", str(caught.exception))

    def test_with_build_returns_a_new_value_and_leaves_the_original_alone(self):
        """The pre-build claims of a run are genuinely not about any artifact.

        If `with_build` mutated, a caller holding the attribution from before the
        build would find its claims retroactively naming an APK that did not exist
        when they were established. Returning a new value is what makes "recorded
        before the build" a property of the claim rather than of the moment it was
        serialised.
        """
        before = Attribution(WHEN, "440")

        after = before.with_build(BUILD)

        self.assertIsNone(before.build_sha256)
        self.assertEqual(after.build_sha256, BUILD)
        self.assertIsNot(after, before)
        self.assertEqual((after.recorded_at, after.version), (WHEN, "440"))

    def test_an_attribution_cannot_be_edited_in_place(self):
        """Frozen, so `bind_build` is a replacement and can be refused.

        The re-bind guard reads the current value and decides. That decision is
        only enforceable if nothing can assign the field behind it.
        """
        attribution = Attribution(WHEN, "440")

        with self.assertRaises(FrozenInstanceError):
            attribution.build_sha256 = BUILD  # type: ignore[misc]

    def test_apply_attributes_a_post_build_claim_with_all_three(self):
        """`apply` is `attributed` with the run's own values, and must stay so."""
        claim = Attribution(WHEN, "440", BUILD).apply(
            claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)
        )

        self.assertEqual(claim.recorded_at, WHEN)
        self.assertEqual(claim.version, "440")
        self.assertEqual(claim.build_sha256, BUILD)

    def test_apply_drops_the_build_hash_for_a_pre_apply_claim(self):
        """The phase rule reaches through `apply`, which is the ledger's only path.

        `record` calls `apply`, never `attributed` directly. An `apply` that
        passed the hash straight to `replace` would raise out of the ledger for
        every pre-apply claim of every build-bound run — the guard firing as a
        crash instead of as a rule.
        """
        claim = Attribution(WHEN, "440", BUILD).apply(
            claim_for(HOOK, EvidenceKind.ANCHOR_UNIQUE)
        )

        self.assertIsNone(claim.build_sha256)
        self.assertEqual(claim.version, "440")


# --------------------------------------------------------------- the ledger wiring


class LedgerAttributionTests(unittest.TestCase):
    """One place per run that can forget, rather than one per builder."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def ledger(self, attribution: Attribution | None = None, name: str = "evidence.jsonl"):
        ledger = EvidenceLedger(self.tmp / name, attribution=attribution)
        ledger.register(Subject(HOOK, "mechanical", descriptor="LX/05t2;"))
        return ledger

    def rows(self, name: str = "evidence.jsonl") -> list[dict[str, Any]]:
        text = (self.tmp / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_a_recorded_claim_comes_back_attributed(self):
        """`record` returns the claim it stored, not the one it was handed.

        Callers keep the return value — `probes.py` does — and a caller holding
        the unattributed original would compute a `claim_id` for a claim that is
        not the one in the file, which is how a supersede chain comes to name a
        parent nothing can find.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))

        stored = ledger.record(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE))

        self.assertEqual(stored.version, "440")
        self.assertEqual(stored.recorded_at, WHEN)
        self.assertEqual(ledger.claims, (stored,))
        self.assertEqual(ledger.claims_for(HOOK)[0].claim_id, stored.claim_id)

    def test_the_attribution_reaches_the_file_and_not_only_the_object(self):
        """The file is the artifact; the object lives for the length of a run.

        Attributing in memory and writing the original would leave every ledger on
        disk exactly as unjoinable as before, and the in-memory report would say
        otherwise for as long as anyone was watching.
        """
        ledger = self.ledger(Attribution(WHEN, "440", BUILD))

        ledger.record(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE))

        (row,) = self.rows()
        self.assertEqual(row["version"], "440")
        self.assertEqual(row["recorded_at"], WHEN)
        self.assertEqual(row["build_sha256"], BUILD)

    def test_a_ledger_with_no_attribution_writes_exactly_what_it_was_given(self):
        """Unchanged behaviour for every caller that has no run identity.

        Tests construct bare ledgers, and so does any offline tool. The field is
        additive: a ledger that was not told what run it belongs to must produce
        the same bytes it produced before the field existed.
        """
        ledger = self.ledger()
        claim = claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)

        stored = ledger.record(claim)

        self.assertEqual(stored, claim)
        self.assertEqual(stored.claim_id, claim.claim_id)
        (row,) = self.rows()
        self.assertEqual(as_bytes(row), as_bytes(claim.to_dict()))
        self.assertNotIn("version", row)
        self.assertNotIn("build_sha256", row)
        self.assertEqual(row["recorded_at"], "")

    def test_a_claim_that_already_names_a_version_keeps_its_own(self):
        """The escape hatch: a `differential` is about two versions, not one.

        The ledger holds a single version and cannot express "439 against 440". A
        claim that arrived carrying its own answer to that question is the only
        one that knows better than the run, so overwriting it would replace a true
        statement about a comparison with a false one about a port.
        """
        ledger = self.ledger(Attribution(WHEN, "440", BUILD))
        claim = replace(
            claim_for(HOOK, EvidenceKind.DIFFERENTIAL), version="439 -> 440"
        )

        stored = ledger.record(claim)

        self.assertEqual(stored.version, "439 -> 440")
        self.assertEqual(self.rows()[0]["version"], "439 -> 440")

    def test_a_claim_with_its_own_version_keeps_it_but_is_still_dated(self):
        """Skipped field by field, not whole — and the timestamp is not one of them.

        `record` used to test `claim.version is None` and, when it was not, apply
        nothing at all. So a claim that brought its own version also kept
        `recorded_at=""` — the exact hole this whole change closes, reintroduced
        for the one kind that needs the version exemption. Bringing your own
        version is no reason to be unorderable in time.

        The build hash stays withheld, deliberately: a claim spanning two builds
        cannot name one of them.
        """
        ledger = self.ledger(Attribution(WHEN, "440", BUILD))

        stored = ledger.record(
            replace(claim_for(HOOK, EvidenceKind.DIFFERENTIAL), version="439 -> 440")
        )

        self.assertEqual(stored.version, "439 -> 440")
        self.assertEqual(stored.recorded_at, WHEN)
        self.assertIsNone(stored.build_sha256)
        self.assertNotIn("build_sha256", self.rows()[0])

    def test_a_claim_that_brought_its_own_timestamp_keeps_that_too(self):
        """Positive control: the ledger fills a gap, it does not overwrite."""
        ledger = self.ledger(Attribution(WHEN, "440", BUILD))
        own = replace(
            claim_for(HOOK, EvidenceKind.DIFFERENTIAL),
            version="439 -> 440",
            recorded_at="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(ledger.record(own).recorded_at, "2020-01-01T00:00:00+00:00")

    def test_a_run_attributes_every_kind_it_records(self):
        """Not just the one a test remembered: the ledger has one path in.

        Whatever the kind, whatever the producer, the claim went through `record`
        — that is the argument for attributing there. A version present on probes
        and missing on the four pre-apply kinds would date half a port.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        for kind in EvidenceKind:
            ledger.record(claim_for(HOOK, kind))

        rows = self.rows()
        self.assertEqual(len(rows), len(EvidenceKind))
        for row in rows:
            with self.subTest(kind=row["kind"]):
                self.assertEqual(row["version"], "440")
                self.assertEqual(row["recorded_at"], WHEN)

    def test_an_attributed_ledger_still_refuses_what_it_always_refused(self):
        """Attribution is applied after the rules, not instead of them.

        An unregistered subject and a proposer producing its own evidence are the
        two refusals `record` exists for. A claim rewritten on the way in must not
        be a claim admitted on the way in.
        """
        ledger = EvidenceLedger(
            self.tmp / "strict.jsonl", attribution=Attribution(WHEN, "440")
        )

        with self.assertRaises(EvidenceError) as caught:
            ledger.record(claim_for("hook.unknown", EvidenceKind.RUNTIME_PROBE))
        self.assertIn("not registered", str(caught.exception))
        self.assertFalse((self.tmp / "strict.jsonl").exists())


# ------------------------------------------------------------------- bind_build


class BindBuildTests(unittest.TestCase):
    """The hash arrives after the claims that cannot have it. One run, one artifact."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def ledger(self, attribution: Attribution | None = None) -> EvidenceLedger:
        ledger = EvidenceLedger(self.tmp / "evidence.jsonl", attribution=attribution)
        ledger.register(Subject(HOOK, "mechanical", descriptor="LX/05t2;"))
        return ledger

    def test_binding_names_the_artifact_for_later_claims(self):
        """The ordinary path: build, bind, record what the build proved."""
        ledger = self.ledger(Attribution(WHEN, "440"))

        ledger.bind_build(BUILD)
        stored = ledger.record(claim_for(HOOK, EvidenceKind.STATIC_VERIFIED))

        self.assertEqual(stored.build_sha256, BUILD)

    def test_claims_recorded_before_the_bind_keep_no_build_hash(self):
        """The entire reason `bind_build` is not a constructor argument.

        The APK does not exist when the ledger is opened. A run records its
        pre-apply evidence, then builds, then records what the build proved — and
        the first group is not about the artifact. Back-filling them would turn
        "established before there was a build" into "checked against this APK",
        which is a claim nobody made.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        early = ledger.record(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE))

        ledger.bind_build(BUILD)
        late = ledger.record(claim_for(HOOK, EvidenceKind.STATIC_VERIFIED))

        self.assertIsNone(early.build_sha256)
        self.assertEqual(late.build_sha256, BUILD)
        rows = [
            json.loads(line)
            for line in (self.tmp / "evidence.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertNotIn("build_sha256", rows[0])
        self.assertEqual(rows[1]["build_sha256"], BUILD)

    def test_a_pre_apply_claim_after_the_bind_still_carries_no_hash(self):
        """The phase rule outranks the ledger's knowledge.

        A run can bind and then record a late pre-apply claim — a hook re-checked
        after the build, a waiver. Knowing the hash does not make the fact one
        about the APK.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        ledger.bind_build(BUILD)

        stored = ledger.record(claim_for(HOOK, EvidenceKind.ANCHOR_UNIQUE))

        self.assertIsNone(stored.build_sha256)
        self.assertEqual(stored.version, "440")

    def test_rebinding_the_same_hash_is_accepted(self):
        """Idempotence, because two call sites naming one artifact is not a conflict.

        The build stage may reasonably bind from more than one place — a report, a
        re-read, a retry of the same build. Refusing an identical hash would turn
        a harmless repetition into a failed port after the APK was already built.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        ledger.bind_build(BUILD)

        ledger.bind_build(BUILD)  # would raise if identity were treated as conflict

        self.assertEqual(
            ledger.record(claim_for(HOOK, EvidenceKind.STATIC_VERIFIED)).build_sha256,
            BUILD,
        )

    def test_rebinding_a_different_hash_is_refused(self):
        """Every claim individually true, the set describing no artifact that existed.

        That is the shape of this failure and it is why the refusal is worth a
        raise rather than a warning: a ledger whose early claims name build A and
        whose late ones name build B is internally consistent, passes every other
        check here, and is evidence about nothing.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        ledger.bind_build(BUILD)

        with self.assertRaises(EvidenceError) as caught:
            ledger.bind_build(OTHER_BUILD)

        self.assertIn("One run, one artifact", str(caught.exception))
        self.assertIn(BUILD, str(caught.exception))
        self.assertIn(OTHER_BUILD, str(caught.exception))

    def test_a_refused_rebind_leaves_the_first_hash_in_place(self):
        """Refusing halfway would be worse than not refusing.

        The guard raises; what it must not do is raise *after* replacing the
        value. A caller that swallowed the error — and the build stage has no
        `try` here, but a future one might — would otherwise continue with the
        second hash and the refusal would have achieved nothing.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))
        ledger.bind_build(BUILD)

        with self.assertRaises(EvidenceError):
            ledger.bind_build(OTHER_BUILD)

        self.assertEqual(
            ledger.record(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE)).build_sha256,
            BUILD,
        )

    def test_binding_a_ledger_with_no_attribution_is_a_no_op(self):
        """Unlabelled runs and every test in this tree call it. It must not raise.

        The build stage binds unconditionally when the report has a hash, and it
        does not know whether the run was labelled. Raising here would make
        `--version` the difference between a port that finishes and one that dies
        after the APK is built.
        """
        ledger = self.ledger()

        ledger.bind_build(BUILD)
        ledger.bind_build(OTHER_BUILD)  # not even the conflict applies

        stored = ledger.record(claim_for(HOOK, EvidenceKind.STATIC_VERIFIED))
        self.assertIsNone(stored.build_sha256)
        self.assertIsNone(stored.version)

    def test_binding_does_not_touch_the_version_or_the_timestamp(self):
        """`with_build` replaces one field; the other two came from the caller."""
        ledger = self.ledger(Attribution(WHEN, "440"))
        ledger.bind_build(BUILD)

        stored = ledger.record(claim_for(HOOK, EvidenceKind.RUNTIME_PROBE))

        self.assertEqual((stored.version, stored.recorded_at), ("440", WHEN))


# --------------------------------------------------------------------- the driver


class PortCase(DriverCase):
    """A three-hook run and the verifier report the build stage reads.

    `run_command` is stubbed by `DriverCase`, so no APK is ever produced and the
    report is written by hand where `build.py` would have left it.
    """

    #: The report `verify_build` writes for the three-DEX fixture, plus the digest
    #: of the APK it checked. Same symbols as `test_static_verified`'s copy, since
    #: they are the same three payloads.
    VERIFICATION: dict[str, Any] = {
        "schema_version": 1,
        "passed": True,
        "apk_sha256": BUILD,
        "host_hooks": {
            "classes.dex": {"Lcom/dfinstagram/startapp; setContext": True},
            "classes3.dex": {"Lcom/dfinstagram/SettingsWrapper; <init>": True},
            "classes10.dex": {
                "Lcom/dfinstagram/adv_settings; noteEndpoint": True,
                "Lcom/dfinstagram/hooks; replaceEndpoint": True,
            },
        },
    }

    HOOKS = (CONTEXT_HOOK, ACTION_BAR_HOOK, ENDPOINT_HOOK)

    def write_verification(self, **overrides: Any) -> None:
        report = {**self.VERIFICATION, **overrides}
        for key, value in list(report.items()):
            if value is None:
                del report[key]
        out = self.base / "run"
        out.mkdir(parents=True, exist_ok=True)
        (out / "dfinsta.verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    def recorded(self, out: str = "run") -> list[dict[str, Any]]:
        text = (self.base / out / "evidence.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertTrue(rows, "the run recorded no evidence at all")
        return rows


class PortAttributionTests(PortCase):
    """A real `port`, and the JSONL it leaves behind.

    Everything above tests a library. This is the class that says the library is
    reached: `port` builds an `Attribution` when `--version` is given, hands it to
    the ledger, and the build stage binds the hash out of the verifier's report.
    Any one of those three links missing leaves a ledger identical to the ones
    that could not be joined to anything.
    """

    def test_every_claim_of_a_labelled_run_carries_the_version_and_the_timestamp(self):
        """The join that did not exist, end to end.

        Nine claims — three hooks, two pre-apply kinds each and one static — and
        every one of them names 439 and says when. Before this a reader had the
        filename and nothing else, and the filename is chosen by whoever copies
        the file.
        """
        fixture = self.three_dex_fixture()
        self.write_verification()

        result = self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        self.assertIs(result.ok, True, result.stopped_because)
        rows = self.recorded()
        self.assertEqual(
            sorted({row["kind"] for row in rows}),
            ["anchor_unique", "registers_safe", "static_verified"],
        )
        for row in rows:
            with self.subTest(hook=row["hook_id"], kind=row["kind"]):
                self.assertEqual(row["version"], "439")
                self.assertEqual(row["recorded_at"], STAMP)

    def test_pre_apply_claims_carry_no_build_hash_and_static_ones_do(self):
        """One run, two phases, and the difference visible in the file.

        This is the whole design in one assertion: the claims established before
        the build name no artifact, the claims about the build name exactly the
        APK the verifier checked, and both are attributed to the same port.
        """
        fixture = self.three_dex_fixture()
        self.write_verification()

        self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        rows = self.recorded()
        static = [row for row in rows if row["kind"] == "static_verified"]
        pre_apply = [row for row in rows if row["kind"] != "static_verified"]
        self.assertEqual(len(static), len(self.HOOKS))
        self.assertEqual(len(pre_apply), 2 * len(self.HOOKS))
        for row in pre_apply:
            with self.subTest(hook=row["hook_id"], kind=row["kind"]):
                self.assertNotIn("build_sha256", row)
        for row in static:
            with self.subTest(hook=row["hook_id"]):
                self.assertEqual(row["build_sha256"], BUILD)

    def test_the_hash_is_the_verifier_report_s_and_is_not_re_derived(self):
        """The digest is of the bytes the verifier checked, taken from its report.

        No APK exists in this run at all — `run_command` is stubbed, so nothing
        was assembled — and the claims still name a build. That is the point:
        re-hashing the output path would answer a different question if anything
        touched the file between the verification and the ledger, and here it
        would answer no question at all.
        """
        fixture = self.three_dex_fixture()
        self.write_verification(apk_sha256=OTHER_BUILD)

        result = self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        self.assertFalse(Path(result.artifacts["apk"]).exists())
        static = [row for row in self.recorded() if row["kind"] == "static_verified"]
        self.assertEqual({row["build_sha256"] for row in static}, {OTHER_BUILD})

    def test_a_report_with_no_digest_leaves_the_claims_unbound_and_the_run_intact(self):
        """A missing hash costs the join, never the port.

        `apk_sha256` is one field of a report with twenty; an older verifier, or
        one that changed its mind about the name, must not stop a build that
        already passed its own verification. The claims are still recorded, still
        versioned, and simply do not name an artifact.
        """
        fixture = self.three_dex_fixture()
        self.write_verification(apk_sha256=None)

        result = self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        self.assertIs(result.ok, True, result.stopped_because)
        rows = self.recorded()
        self.assertEqual(result.artifacts["static_verified"], "3/3")
        for row in rows:
            with self.subTest(hook=row["hook_id"], kind=row["kind"]):
                self.assertNotIn("build_sha256", row)
                self.assertEqual(row["version"], "439")

    def test_an_unlabelled_run_records_claims_with_no_attribution_and_does_not_crash(self):
        """`--version` is optional and stays optional.

        An offline port with no run identity is a real mode — every test in
        `test_driver` uses it — and it must produce exactly the ledger it produced
        before the field existed: no version, no build hash, and `recorded_at`
        empty, even though the verifier report here carries a perfectly good
        digest for `bind_build` to have used.
        """
        fixture = self.three_dex_fixture()
        self.write_verification()

        result = self.run_port(fixture, list(self.HOOKS))

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.artifacts["static_verified"], "3/3")
        for row in self.recorded():
            with self.subTest(hook=row["hook_id"], kind=row["kind"]):
                self.assertNotIn("version", row)
                self.assertNotIn("build_sha256", row)
                self.assertEqual(row["recorded_at"], "")

    def test_a_labelled_run_without_a_timestamp_is_refused_before_anything_happens(self):
        """The two halves of an attribution are required together.

        `port` already refused this for the cost ledger, and the ledger's
        `Attribution` now depends on the same pairing: a version with no timestamp
        would be an attribution the constructor rejects, discovered at the first
        `record` rather than at the argument that caused it.
        """
        fixture = self.three_dex_fixture()

        with self.assertRaises(DriverError) as caught:
            self.run_port(fixture, list(self.HOOKS), version="439", recorded_at="")

        self.assertIn("--version needs --recorded-at", str(caught.exception))
        self.assertEqual(self.commands, [])


# ------------------------------------------------------------------- known gaps


class ClosedGapTests(unittest.TestCase):
    """Three defects this file found while it was being written, all fixed.

    Each was written first as a pin on what the code then did, so the fix
    announced itself as a failure here rather than as a quiet change in what a
    ledger says. They now pin the corrected behaviour.
    """

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def ledger(self, attribution: Attribution | None = None) -> EvidenceLedger:
        ledger = EvidenceLedger(self.tmp / "evidence.jsonl", attribution=attribution)
        ledger.register(Subject(HOOK, "mechanical", descriptor="LX/05t2;"))
        return ledger

    def test_the_differential_escape_hatch_now_has_a_producer(self):
        """`record` keeps a claim's own version, and `differential` finally sets one.

        The branch was written for `differential`, which compares two versions and
        cannot be described by a single one — and nothing set a version, so the
        branch had no producer and a differential recorded through an attributed
        ledger was stamped with the run's one version, exactly what it was written
        to prevent. The same complete-but-disconnected shape as the feature gate
        with no producer and the rulings with no consumer.

        The version is the CURRENT one: "440 did not regress" is a fact about 440,
        with the baseline named in `detail`.
        """
        from dfinsta_pipeline.differential import compare  # noqa: PLC0415

        claim = compare(HOOK, {}, {}, baseline_version="439", current_version="440",
                        actor="device:P3227J000775")
        self.assertIs(claim.kind, EvidenceKind.DIFFERENTIAL)
        self.assertEqual(claim.version, "440")
        self.assertEqual(claim.detail["baseline_version"], "439")

        # Recorded against a ledger whose run is about something else entirely:
        # the comparison keeps its own version rather than being overwritten.
        stored = self.ledger(Attribution(WHEN, "441", BUILD)).record(claim)
        self.assertEqual(stored.version, "440")
        # ...but it is still DATED, and still does not name one of two builds.
        self.assertEqual(stored.recorded_at, WHEN)
        self.assertIsNone(stored.build_sha256)

    def test_a_differential_version_cannot_disagree_with_its_detail(self):
        """Derived from `detail`, so the two are one statement rather than two.

        A branch that forgot `base_detail` would produce an unversioned claim,
        which is exactly as unjoinable as the claims this whole change fixes — so
        it raises rather than defaulting.
        """
        from dfinsta_pipeline import differential  # noqa: PLC0415

        with self.assertRaises(EvidenceError) as caught:
            differential._claim(
                HOOK, differential.Verdict.PASSED, "device:1", "s",
                {"baseline_version": "439"},
            )
        self.assertIn("current_version", str(caught.exception))

    def test_a_malformed_hash_is_refused_where_it_still_names_its_source(self):
        """Validated at `Attribution`, not three steps later inside a claim builder.

        `with_build` and `bind_build` used to take any string, so an uppercase
        digest was accepted at the call that knew where it came from and rejected
        at the next `record` — a failure surfacing as far as possible from its
        cause, out of a stretch of the build stage with no `try` around it.
        """
        ledger = self.ledger(Attribution(WHEN, "440"))

        with self.assertRaises(EvidenceError) as caught:
            ledger.bind_build(BUILD.upper())
        self.assertIn("must be a lowercase", str(caught.exception))

        # Refused, so nothing was bound: the next claim is unaffected rather than
        # carrying a half-applied attribution.
        recorded = ledger.record(claim_for(HOOK, EvidenceKind.STATIC_VERIFIED))
        self.assertIsNone(recorded.build_sha256)
        self.assertEqual(recorded.version, "440")

    def test_the_constructor_refuses_it_too(self):
        """Positive control: the rule lives in both places on purpose."""
        with self.assertRaises(EvidenceError):
            Attribution(WHEN, "440", BUILD.upper())


class PortClosedGapTests(PortCase):
    """The reachable form of the hash gap, driven through the real `port`."""

    def test_an_uppercase_digest_costs_the_evidence_and_not_the_run(self):
        """A build that passed its own verification must not die over bookkeeping.

        The build stage used to bind when `apk_sha256` was a 64-character string —
        length only, no alphabet — while `EvidenceClaim` required lowercase hex.
        A report spelled the other way therefore reached `bind_build` and the
        first `static_verified` claim raised `EvidenceError` out of a stretch with
        no `try` around it; `EvidenceError` is a `ValueError`, not a
        `DriverError`, so it escaped `port` altogether — no `[cost]` receipt, no
        message naming the report, a traceback out of a run whose APK was already
        built and verified.

        Two lines up the same stage says an unreadable report costs "evidence, not
        correctness". This is now the same: the run finishes, the claims simply do
        not name the APK, and the operator is told which value was wrong.
        """
        fixture = self.three_dex_fixture()
        self.write_verification(apk_sha256=BUILD.upper())

        result = self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertIn("claims will not name the APK", self.printed)
        # The evidence is still recorded and still versioned — only the join to
        # the artifact is lost, which is the thing that actually went missing.
        claims = self.recorded()
        static = [c for c in claims if c["kind"] == "static_verified"]
        self.assertTrue(static)
        self.assertTrue(all(c.get("build_sha256") is None for c in static))
        self.assertTrue(all(c["version"] == "439" for c in static))

    def test_the_same_report_in_lowercase_is_the_control(self):
        """The digest is the only difference, so the case is the whole cause."""
        fixture = self.three_dex_fixture()
        self.write_verification(apk_sha256=BUILD)

        result = self.run_port(fixture, list(self.HOOKS), version="439", recorded_at=STAMP)

        self.assertIs(result.ok, True, result.stopped_because)
        self.assertEqual(result.artifacts["static_verified"], "3/3")
        static = [c for c in self.recorded() if c["kind"] == "static_verified"]
        self.assertTrue(all(c["build_sha256"] == BUILD for c in static))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

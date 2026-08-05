"""Fast checks on the registered-replay harness, run without Temporal or apktool.

The harness in `tests/integration/test_registered_replay_harness.py` costs about
an hour of compute per target and only asserts at the end. Its first version
carried two one-line static mistakes -- it compared the Workflow's stages against
a vocabulary from the sibling harness, and read a history key under a name
`_history_evidence` does not emit -- and both would have aborted *after* the run,
which is the most expensive place to find anything.

So these bind the harness's tables to the things they are tables of. Nothing here
starts a server, a worker or a subprocess.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from dfinsta_pipeline.replay_contracts import (
    REPLAY_STAGE_ORDER,
    REPLAY_STAGES_WITHOUT_FRAMEWORK,
)

from tests.integration.test_real_replay_harness import TARGETS
from tests.integration.test_registered_replay_harness import (
    ASSERTED_HISTORY_KEYS,
    HISTORY_BUDGET_BYTES,
    TARGET_EVIDENCE_KEYS,
    _CONFIRM,
    _history_evidence,
    _sample_heartbeats,
    _worst_heartbeat_gaps,
    derived_verification_identifiers,
    expected_activity_sequence,
    expected_stages,
    worker_command,
)


class StageVocabularyTests(unittest.TestCase):
    def test_expected_stages_uses_the_workflow_vocabulary(self) -> None:
        """`framework` is the sibling harness's word and is unreachable here.

        `ReplayRunResultV1` rejects any stage outside `REPLAY_STAGE_ORDER`, so a
        comparison against `('framework', ...)` could not have passed on 430 no
        matter what the Workflow did.
        """
        self.assertEqual(expected_stages(TARGETS[340]), REPLAY_STAGES_WITHOUT_FRAMEWORK)
        self.assertEqual(expected_stages(TARGETS[430]), REPLAY_STAGE_ORDER)
        for target in (340, 430):
            for stage in expected_stages(TARGETS[target]):
                self.assertIn(stage, REPLAY_STAGE_ORDER, stage)

    def test_the_two_vocabularies_agree_only_without_a_framework_stage(self) -> None:
        """Why a 340-only check would have missed it."""
        from tests.integration.test_real_replay_harness import stage_order

        self.assertEqual(stage_order(TARGETS[340]), expected_stages(TARGETS[340]))
        self.assertNotEqual(stage_order(TARGETS[430]), expected_stages(TARGETS[430]))

    def test_the_expected_activity_sequence_names_registered_activities(self) -> None:
        """Each name must be an Activity the worker actually registers."""
        from dfinsta_pipeline import worker as worker_module
        from temporalio import activity

        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined,union-attr]
            for fn in worker_module.REGISTERED_ACTIVITIES
        }
        for target in (340, 430):
            sequence = expected_activity_sequence(TARGETS[target])
            for name in sequence:
                self.assertIn(name, registered, name)
            # The three the registered path adds, which `stages_completed` can
            # never mention, are all present.
            self.assertIn("prepare_replay_plan_activity", sequence)
            self.assertIn("prepare_replay_verification_gate_activity", sequence)
            self.assertIn("admit_replay_verification_grant_activity", sequence)
        self.assertEqual(len(expected_activity_sequence(TARGETS[430])), 9)
        self.assertEqual(len(expected_activity_sequence(TARGETS[340])), 8)


class HistoryEvidenceTests(unittest.TestCase):
    """`_history_evidence` must emit every key the assertions read."""

    class _Event:
        def __init__(self) -> None:
            pass

        def HasField(self, _name: str) -> bool:
            return False

    class _History:
        def __init__(self, payload: str) -> None:
            self.events: list[object] = []
            self._payload = payload

        def to_json(self) -> str:
            return self._payload

    def _evidence(self, payload: str, control: str) -> dict[str, object]:
        history = self._History(payload)
        with mock.patch(
            "temporalio.api.history.v1.History",
            return_value=mock.Mock(SerializeToString=lambda: b""),
        ):
            return _history_evidence(history, control)

    def test_every_asserted_key_is_emitted(self) -> None:
        """Read out of the harness source, not out of a list anyone maintains.

        The original defect was an assertion naming `contains_source_manifest_marker`
        against an emitter producing `contains_source_tree_marker`. A hand-written
        list of asserted keys would have had to be updated by the same person who
        wrote the mismatched assertion, so it is derived instead.
        """
        import ast

        module = Path(
            "tests/integration/test_registered_replay_harness.py"
        ).resolve()
        tree = ast.parse(module.read_text(encoding="utf-8"))
        read: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "history_evidence"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                read.add(node.slice.value)
        self.assertTrue(read, "found no history_evidence[...] reads to check")
        self.assertEqual(read, set(ASSERTED_HISTORY_KEYS))

        evidence = self._evidence(json.dumps({"events": []}), "a" * 64)
        missing = read - frozenset(evidence)
        self.assertEqual(missing, set(), f"assertions read keys nothing emits: {missing}")

    def test_the_control_is_reported_from_the_decoded_surface(self) -> None:
        """The positive control has to come out of the base64, not the raw JSON."""
        import base64

        control = "c" * 64
        blob = base64.b64encode(json.dumps({"sha": control}).encode()).decode()
        payload = json.dumps({"events": [{"payloads": [{"data": blob}]}]})
        evidence = self._evidence(payload, control)
        self.assertGreaterEqual(evidence["decoded_payloads"], 1)
        self.assertTrue(evidence["control_found_in_surface"])
        self.assertFalse(evidence["control_visible_in_raw_json"])

    def test_the_budget_is_measured_over_the_json_bytes_it_reports(self) -> None:
        payload = json.dumps({"events": []})
        evidence = self._evidence(payload, "d" * 64)
        self.assertEqual(evidence["json_bytes"], len(payload.encode("utf-8")))
        self.assertEqual(evidence["budget_bytes"], HISTORY_BUDGET_BYTES)
        self.assertEqual(
            evidence["within_budget"], evidence["json_bytes"] <= HISTORY_BUDGET_BYTES
        )


class HarnessWiringTests(unittest.TestCase):
    def test_the_confirmation_pattern_matches_what_the_client_prints(self) -> None:
        """Parsed rather than recomputed, so the pattern must match `describe`."""
        from dfinsta_pipeline.submission import CONFIRMATION_LENGTH

        digest = "0123456789abcdef" * 4
        line = f"to answer, pass --confirm {digest[:CONFIRMATION_LENGTH]}"
        match = _CONFIRM.search(f"subject\n    {digest}\n\n{line}")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), digest[:CONFIRMATION_LENGTH])
        self.assertTrue(digest.startswith(match.group(1)))

    def test_the_worker_command_supplies_both_runtime_inputs(self) -> None:
        """The bug this harness found in the first minute must stay fixed."""
        command = worker_command(
            Path("/state"),
            "queue",
            "build",
            Path("/usr/bin/java"),
            endpoint="localhost:7233",
            attempts_root=Path("/attempts"),
        )
        self.assertIn("--source-root", command)
        self.assertIn("--executor-path", command)
        self.assertIn("-m", command)
        self.assertIn("dfinsta_pipeline.worker", command)

    def test_derived_identifiers_are_the_ones_the_design_doc_pinned(self) -> None:
        """F1's deferred pins, computable at rest because derivation is pure."""
        for target in (340, 430):
            run_id = f"real-replay-{target}-run"
            self.assertEqual(
                derived_verification_identifiers(run_id),
                {
                    "grant_id": f"real-replay-{target}-run-final-verification-grant",
                    "gate_id": f"real-replay-{target}-run-final-verification-gate",
                    "capability_id": f"real-replay-{target}-run-final-verification-decode",
                },
            )

    def test_the_evidence_schema_names_the_worker_outcome(self) -> None:
        """A SIGKILLed worker must not produce evidence identical to a clean exit."""
        self.assertIn("worker_outcome", TARGET_EVIDENCE_KEYS)


class HeartbeatSamplingTests(unittest.TestCase):
    """The heartbeat reader, exercised at rest.

    Two static defects in this harness -- a renamed key and a borrowed stage
    vocabulary -- each cost a full run to discover, and a run is an hour. So the
    protobuf walk gets a fast test against a hand-built description rather than
    being first exercised at minute fifty of a real port.
    """

    @staticmethod
    def _description(activities: list[tuple[str, bytes | None]]) -> object:
        from temporalio.api.common.v1 import Payload
        from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse

        raw = DescribeWorkflowExecutionResponse()
        for name, payload in activities:
            pending = raw.pending_activities.add()
            pending.activity_type.name = name
            pending.attempt = 1
            if payload is not None:
                pending.heartbeat_details.payloads.append(Payload(data=payload))
        return mock.Mock(raw_description=raw)

    def test_a_stage_reporting_its_gap_is_read_back(self) -> None:
        details = {
            "stage": "decode",
            "beats": 2,
            "elapsed_seconds": 61.4,
            "worst_gap_seconds": 30.9,
        }
        samples = _sample_heartbeats(
            self._description(
                [("replay_decode_stage_activity", json.dumps(details).encode("utf-8"))]
            )
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["activity"], "replay_decode_stage_activity")
        self.assertEqual(samples[0]["details"], details)
        self.assertEqual(_worst_heartbeat_gaps(samples), {"decode": 30.9})

    def test_a_stage_that_has_not_beaten_yet_is_recorded_without_details(self) -> None:
        """A pending activity with no heartbeat must not look like a zero gap."""
        samples = _sample_heartbeats(
            self._description([("replay_verify_final_apk_stage_activity", None)])
        )
        self.assertEqual(len(samples), 1)
        self.assertNotIn("details", samples[0])
        self.assertEqual(_worst_heartbeat_gaps(samples), {})

    def test_an_undecodable_payload_is_recorded_rather_than_raised(self) -> None:
        """A measurement that cannot be read must not fail a port that is fine."""
        samples = _sample_heartbeats(
            self._description([("replay_decode_stage_activity", b"\xff not json")])
        )
        self.assertIn("details_error", samples[0])
        self.assertEqual(_worst_heartbeat_gaps(samples), {})

    def test_the_worst_gap_per_stage_wins_over_the_run(self) -> None:
        """The number a timeout is set from is the maximum, not the latest."""
        samples = [
            {"details": {"stage": "decode", "worst_gap_seconds": 30.9}},
            {"details": {"stage": "decode", "worst_gap_seconds": 12.0}},
            {"details": {"stage": "build", "worst_gap_seconds": 41.2}},
            {"details": "not a mapping"},
            {},
        ]
        self.assertEqual(
            _worst_heartbeat_gaps(samples), {"decode": 30.9, "build": 41.2}
        )

    def test_the_evidence_schema_carries_the_measurement(self) -> None:
        """Sampled and not recorded is the same as not sampled."""
        self.assertIn("stage_heartbeats", TARGET_EVIDENCE_KEYS)
        self.assertIn("worst_heartbeat_gap_seconds", TARGET_EVIDENCE_KEYS)


if __name__ == "__main__":
    unittest.main()

"""The committed replay-History corpus: what is in it, and what may not be.

`tests/test_phase_a_history_corpus.py` says why a corpus exists at all, and it is
worth repeating because it is the whole point: a test that *generates* the
History it replays can only confirm that a Workflow agrees with itself. Every
Workflow here is `versioning_behavior=PINNED`, so a change to a command sequence
breaks replay of Histories that were already durably recorded — and nothing
notices until a real run is resumed after a deploy. A committed fixture keeps
failing until the change is made compatible with the shape on disk.

This module holds the parts of the corpus that both the test and the capture tool
must agree on, so they cannot drift: which files exist, which workflow execution
each came from, what each is pinned at, and what a fixture may not contain.

===============================================================================
  WHAT IS SANITISED, AND WHY IT IS NOT CONFIDENTIALITY
===============================================================================

There is nothing secret in these payloads. The reason to sanitise is
**portability and determinism**: a fixture containing `/tmp/tmpab12cd` or
`1292223@thinkpad` records the machine that captured it, cannot be reproduced
anywhere else, and turns an absence assertion into a machine-specific accident.

Exactly one thing is rewritten after capture, by
`tools/capture_history_corpus.py`: every protobuf field named `identity`. Temporal
defaults both client and worker identity to `pid@hostname`, which lands in
`WorkflowExecutionStarted`, in every `WorkflowTaskStarted` and
`ActivityTaskStarted`, and in the metadata of every Update — around forty-five
events in a full run. It is never read on replay.

Deliberately **not** rewritten:

* `eventTime`. The replayer derives timer fire times from recorded event times, so
  a rewritten clock would change what is being replayed.
* Run UUIDs (`originalExecutionRunId`, `firstExecutionRunId`, `requestId`). They
  are random per capture, not machine-derived, and they are what makes the events
  internally coherent.
* Payload bodies. A fixture whose payloads are all `null` proves nothing: the
  command stream a Workflow produces depends on what the Activities returned, so
  the payloads *are* the fixture. Everything environment-specific was instead
  kept out at capture time — the corpus runs use a neutral actor, fixed rationale
  strings, and run ids that name the corpus rather than a person or a machine.

`leaks()` is what makes the paragraph above checkable rather than a promise, and
`tests/test_history_corpus.py` runs it over every committed fixture with a
planted-leak control so it cannot pass by searching nothing.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import NamedTuple

from tests.history_search import decoded_payload_count


#: Substituted for every `identity` field at capture. A literal, so a fixture can
#: be asserted to carry it — an identity field that is *missing* and one that was
#: rewritten look the same to a search for what must be absent.
CAPTURE_IDENTITY = "dfinsta-history-corpus"


class Fixture(NamedTuple):
    """One committed History, and the execution identifiers it does not carry.

    `WorkflowHistory.to_json()` drops the workflow id (it serialises the event
    list alone), so it has to be recorded next to the file rather than read out
    of it. `sha256` pins the bytes: the replay tests below pass against a fixture
    regenerated from current code, which is precisely the self-consistency trap
    the corpus exists to escape, so silently re-capturing instead of fixing a
    compatibility break must fail the suite.
    """

    filename: str
    workflow_id: str
    sha256: str


#: Every committed History. `tests/test_history_corpus.py` asserts this agrees
#: with the directory in both directions, so a file added without a row here (or
#: a row without a file) fails rather than being quietly skipped.
FIXTURES = (
    Fixture(
        "phase_a_completed_v1.json",
        "run-history",
        "aab03cb8104e5ef5351d26afb2ab03d4583650ce6088b08ca89fd3f2f8adcb40",
    ),
    Fixture(
        "phase_a_open_at_approval_gate_v1.json",
        "corpus-phase-a-open",
        "f3c7bfef02c08efdfdcf25157d2e26389359163729e9680f03d48f4042fd8ce7",
    ),
    Fixture(
        "replay_run_completed_v1.json",
        "corpus-replay-completed",
        "d2055f12a27ec294136370f00a9ee3f4084802f93ebfa1d1127c48f85ad1276f",
    ),
    Fixture(
        "replay_run_open_at_verification_gate_v1.json",
        "corpus-replay-open",
        "2cc9f4925f681230d1a4f338c51b726db8af525c148c76a8cb5af3f0e36a9142",
    ),
    Fixture(
        "feature_gate_completed_v1.json",
        "corpus-feature-completed",
        "91b3150131ec8ca6c5614d80ad2c0551f2f23f1e9812e71c37fb319b5710aee0",
    ),
    Fixture(
        "feature_gate_open_v1.json",
        "corpus-feature-open",
        "fbd9e420e690a673dba3c10ac3e2c27ee9fa1e2bcf958809b2f5f6a6bfc6a2bf",
    ),
    Fixture(
        "retirement_gate_completed_v1.json",
        "corpus-retirement-completed",
        "97a1aec84cb8e125cf99de9890b4587138ad007539a2cf33213c7b6a5b6f98f3",
    ),
    Fixture(
        "retirement_gate_open_v1.json",
        "corpus-retirement-open",
        "7645582f0c07c40a1beaa37a87c14813d3f8c43328ca5a26ec902814a6f447cc",
    ),
    Fixture(
        "reversal_gate_completed_v1.json",
        "corpus-reversal-completed",
        "7c521f238ef6ec51e2314d9c536a96d6855959f62e956f2a55a66a63a2443ad0",
    ),
    Fixture(
        "reversal_gate_open_v1.json",
        "corpus-reversal-open",
        "a30a005261a3414db66dde472aae031bfb0bb4d33d343b4b29908d9d8a927661",
    ),
)


def histories_directory() -> Path:
    """Where the fixtures live.

    A function and not a module constant, and `.parent` rather than `.resolve()`,
    because this module is imported by `tests/test_history_corpus.py`, which
    defines the negative-control Workflows — and the Temporal sandbox **re-imports
    the module that defines a Workflow class** when the Replayer validates it.
    `pathlib.Path.resolve` is restricted in that sandbox. A module-level
    `Path(__file__).resolve()` anywhere on that import path turns every control
    into `RuntimeError: Failed validating workflow <name>`, which is the
    pre-existing isolation bug recorded in `docs/IMPLEMENTATION_STATE.md`.
    """

    return Path(__file__).parent / "histories"


#: Event types that end an execution. Anything else means the History was
#: captured while the Workflow was still running -- for the gates in this
#: pipeline, parked in `wait_condition` waiting for a human, which is the state a
#: worker restart has to survive.
TERMINAL_EVENT_TYPES = frozenset(
    {
        "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TIMED_OUT",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CANCELED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED",
        "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW",
    }
)

#: Payload bodies are base64 in the JSON rendering. They are decoded and searched
#: separately, so the encoded form is elided before any regex runs: the base64
#: alphabet includes `/` and every lowercase letter, so scanning it directly
#: would let a chance run of characters read as a filesystem path.
_PAYLOAD_DATA = re.compile(r'("data"\s*:\s*")([A-Za-z0-9+/=]+)(")')

#: Absolute paths, in the four shapes a capture on any developer machine takes.
#: `arnav` is this repository's owner: the Phase A corpus test already forbids it
#: by name, and a fixture naming a person is exactly the kind of thing that gets
#: noticed only after it is published.
_FORBIDDEN_LITERALS = (
    "/home/",
    "/Users/",
    "/tmp/",
    "/var/folders/",
    "/private/var/",
    "C:\\",
    "\\Users\\",
    "PRIVATE_KEY",
    "PASSWORD",
    "SECRET",
    "BEGIN RSA",
    "arnav",
)

_FORBIDDEN_PATTERNS = (
    # `tempfile` names: eight random characters after `tmp`. Caught even without
    # a leading slash, because a bare `tmpab12cd` in a payload is still a
    # reference to a directory that existed on one machine for one minute.
    re.compile(r"\btmp[a-z0-9_]{6,}"),
    # `user@host` in ANY shape, not just Temporal's `pid@hostname`. The pattern
    # required a numeric prefix, so `sam@build07` scanned clean -- and the
    # identity-field assertion that would have caught it only looks at fields
    # named `identity`.
    re.compile(r"\b[A-Za-z0-9._\-]+@[A-Za-z0-9][A-Za-z0-9._\-]*"),
    # A home directory by shape rather than by owner. `~/` and `/data/users/x/`
    # and `/srv/…/workspace/` all scanned clean, and the two values that WERE
    # caught were caught by the literal "arnav" -- a guard that silently narrows
    # the day the owner changes.
    re.compile(r"~/[A-Za-z0-9._\-]"),
    re.compile(r"/(?:home|Users|data/users|export/home)/[A-Za-z0-9._\-]+"),
    re.compile(r"/run/user/\d+"),
    re.compile(r"\buid=\d+"),
    # An absolute path into a build or checkout root, which is what a CI capture
    # would leak.
    re.compile(r"/(?:srv|opt|build|workspace|jenkins)/[A-Za-z0-9._\-]+"),
    # A bare hostname is not recognisable in general, so this catches the two
    # shapes that actually appear: a dotted internal name, and a container id.
    re.compile(r"\b[a-z0-9\-]+\.(?:corp|internal|local|lan)\b"),
    # NO IPv4 pattern, and it was tried. Bounding each octet to 0-255 is not
    # enough: an Instagram version string like `441.0.0.43.81` contains the
    # perfectly valid dotted quad `0.0.43.81`, so the scan flags a version number
    # this repository handles constantly. Same judgement as the hex case below —
    # a scan that cries wolf on ordinary content is a scan somebody turns off,
    # and an address in a replay History is a marginal leak vector next to that.
    # NO bare-hex pattern. A container id is hex, and so is every digest this
    # corpus legitimately carries — CAS URIs, build hashes, the fixtures' own
    # pins. Adding one flagged all 8 fixtures on real content, and a leak scan
    # that cries wolf on every hash is a leak scan nobody runs. A bare hostname
    # is likewise unrecognisable in general; only the dotted internal form above
    # is worth matching.
)


def scannable_surface(history_json: str) -> str:
    """The JSON with payload bodies elided, joined to those bodies decoded.

    Same principle as `tests/history_search.py` -- an assertion against the raw
    JSON alone cannot fail for anything carried inside a payload, which is where
    a leaked path would be -- with the encoded blobs removed rather than left in
    place, so that the regexes above cannot match base64 noise.
    """

    elided = _PAYLOAD_DATA.sub(r"\1\3", history_json)
    bodies = []
    for blob in _PAYLOAD_DATA.findall(history_json):
        try:
            bodies.append(base64.b64decode(blob[1], validate=True).decode("utf-8", "replace"))
        except ValueError:
            # The RAW blob, not `continue`. Dropping it removed the value from the
            # search surface altogether, so a string that is base64-alphabet-legal
            # but not valid base64 -- `/home/arnav/AI/dfinsta` is exactly that --
            # was elided by the substitution above and then never added back, and
            # `leaks()` returned clean. Unreadable is not absent; that conflation
            # is this repository's most repeated defect, and here it silently
            # shrank the very surface a leak would hide in.
            bodies.append(blob[1])
    return "\n".join([elided, *bodies])


def leaks(history_json: str) -> list[str]:
    """Every environment-specific value found. Empty means the fixture travels.

    Returns the offending strings rather than a bool so a failure names what was
    found: `tmp` false positives and real leaks read identically as a count.
    """

    surface = scannable_surface(history_json)
    found = [literal for literal in _FORBIDDEN_LITERALS if literal in surface]
    for pattern in _FORBIDDEN_PATTERNS:
        found.extend(sorted({match.group(0) for match in pattern.finditer(surface)}))
    return found


def searchable_payload_count(history_json: str) -> int:
    """How many payload bodies the search surface actually decoded.

    Zero means every absence assertion over this fixture is vacuous. Re-exported
    from `tests/history_search.py` so the corpus has one answer to that question.
    """

    return decoded_payload_count(history_json)


def payload_encodings(history_json: str) -> set[str]:
    """The distinct `metadata.encoding` values, decoded from their base64.

    Searching decoded payload text only proves anything about payloads that are
    *text*. This is how the test asserts the surface is complete rather than
    assuming it: anything but `json/plain` (and `binary/null`, which carries no
    body at all) is a payload the scan above cannot see into.
    """

    encodings: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            metadata = node.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("encoding"), str):
                encodings.add(
                    base64.b64decode(metadata["encoding"], validate=True).decode(
                        "utf-8", "replace"
                    )
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(history_json))
    return encodings


def identities(history_json: str) -> set[str]:
    """Every `identity` value the History records, at any depth.

    The positive half of the identity check. "No `pid@hostname` anywhere" is
    equally true of a History that records no identity at all, and the two are
    indistinguishable to a search for what must be absent -- so the test asserts
    that identities are present *and* that none of them names a machine.
    """

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "identity" and isinstance(value, str):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(history_json))
    return found


def workflow_type_name(history_json: str) -> str:
    """The Workflow type the History was recorded from, read out of the History.

    The join between a fixture and a registered Workflow class is derived from
    this rather than from a table, so a fixture cannot claim to cover a Workflow
    it was not captured from.
    """

    started = json.loads(history_json)["events"][0]
    return started["workflowExecutionStartedEventAttributes"]["workflowType"]["name"]


def is_closed(history_json: str) -> bool:
    """Whether the execution had ended when the History was captured."""

    return any(
        event["eventType"] in TERMINAL_EVENT_TYPES
        for event in json.loads(history_json)["events"]
    )

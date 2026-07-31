"""Search a Temporal History everywhere a secret could actually hide.

`WorkflowHistory.to_json()` base64-encodes every payload body, so a plaintext
search of that JSON cannot see inside a payload -- which is precisely where a
leaked path, credential or oversized artifact would travel. An assertion like

    self.assertNotIn("PASSWORD", history.to_json())

therefore passes whether or not the secret is present, and cannot fail for the
leak it exists to catch.

Verified against `tests/histories/phase_a_completed_v1.json`: the run's
`subject_sha256` and `run_id` appear nowhere in the raw JSON text, yet five of
its eleven payload blobs contain them once decoded.

Any absence assertion built on this helper MUST be paired with a positive
control -- assert that some value known to live in a payload IS found. Without
one, a decoding change would silently empty the search surface and every
absence assertion would pass vacuously, reintroducing the original bug.
"""

from __future__ import annotations

import base64
import re

_PAYLOAD_DATA = re.compile(r'"data"\s*:\s*"([A-Za-z0-9+/=]+)"')


def history_search_surface(history_json: str) -> str:
    """Return the raw History JSON joined with every decoded payload body."""
    parts = [history_json]
    for blob in _PAYLOAD_DATA.findall(history_json):
        try:
            parts.append(base64.b64decode(blob, validate=True).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


def decoded_payload_count(history_json: str) -> int:
    """How many payload bodies decoded. Zero means the surface is not searchable."""
    decoded = 0
    for blob in _PAYLOAD_DATA.findall(history_json):
        try:
            base64.b64decode(blob, validate=True).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            continue
        decoded += 1
    return decoded

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .contracts import ArtifactRef


class ContentStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        *,
        kind: str,
        data: bytes,
        producer_operation_id: str,
        input_hashes: tuple[str, ...],
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        directory = self.root / "sha256" / digest[:2]
        destination = directory / digest
        directory.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        if destination.read_bytes() != data:
            raise ValueError("Content-addressed artifact collision")
        return ArtifactRef(
            schema_version=1,
            kind=kind,
            sha256=digest,
            size=len(data),
            uri=f"cas://sha256/{digest}",
            producer_operation_id=producer_operation_id,
            input_hashes=input_hashes,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        path = self.root / "sha256" / reference.sha256[:2] / reference.sha256
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.sha256 or len(data) != reference.size:
            raise ValueError("Artifact reference verification failed")
        return data

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from .models import JobCheckpoint


class JobCheckpointStore(Protocol):
    def load(self, job_id: str) -> JobCheckpoint | None: ...
    def save(self, checkpoint: JobCheckpoint) -> None: ...
    def clear(self, job_id: str) -> None: ...


class InMemoryJobCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[str, dict] = {}

    def load(self, job_id: str) -> JobCheckpoint | None:
        payload = self._checkpoints.get(job_id)
        if payload is None:
            return None
        return JobCheckpoint.from_dict(json.loads(json.dumps(payload, ensure_ascii=False)))

    def save(self, checkpoint: JobCheckpoint) -> None:
        self._checkpoints[checkpoint.job_id] = checkpoint.to_dict()

    def clear(self, job_id: str) -> None:
        self._checkpoints.pop(job_id, None)


class JsonJobCheckpointStore:
    """Atomic one-file-per-job checkpoint storage for local resumable workers."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)

    def _path(self, job_id: str) -> Path:
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
        return self.directory / f"job-{digest}.json"

    def load(self, job_id: str) -> JobCheckpoint | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        checkpoint = JobCheckpoint.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if checkpoint.job_id != job_id:
            raise ValueError("Checkpoint job_id does not match requested job")
        return checkpoint

    def save(self, checkpoint: JobCheckpoint) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint.job_id)
        payload = json.dumps(
            checkpoint.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self.directory,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def clear(self, job_id: str) -> None:
        try:
            self._path(job_id).unlink()
        except FileNotFoundError:
            pass

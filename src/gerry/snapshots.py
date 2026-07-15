from __future__ import annotations

import hashlib
import shutil
from datetime import date
from pathlib import Path

import requests

from .domain import DataSnapshot, SourceArtifact


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, election_id: str, effective_date: date) -> DataSnapshot:
        snapshot = DataSnapshot(election_id=election_id, effective_date=effective_date)
        (self.root / str(snapshot.id)).mkdir(parents=True)
        self.save(snapshot)
        return snapshot

    def save(self, snapshot: DataSnapshot) -> None:
        directory = self.root / str(snapshot.id)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "snapshot.json.part"
        temporary.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(directory / "snapshot.json")

    def get(self, snapshot_id) -> DataSnapshot | None:
        path = self.root / str(snapshot_id) / "snapshot.json"
        return DataSnapshot.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None

    def list(self) -> list[DataSnapshot]:
        return [
            DataSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*/snapshot.json"))
        ]

    def import_file(self, snapshot: DataSnapshot, source: str, path: Path) -> SourceArtifact:
        destination = self.root / str(snapshot.id) / path.name
        shutil.copy2(path, destination)
        artifact = SourceArtifact(
            source=source, local_path=str(destination), sha256=sha256_file(destination)
        )
        snapshot.artifacts.append(artifact)
        self.save(snapshot)
        return artifact

    def download(self, snapshot: DataSnapshot, source: str, url: str, filename: str) -> SourceArtifact:
        destination = self.root / str(snapshot.id) / filename
        temporary = destination.with_suffix(destination.suffix + ".part")
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk)
        temporary.replace(destination)
        artifact = SourceArtifact(
            source=source, url=url, local_path=str(destination), sha256=sha256_file(destination)
        )
        snapshot.artifacts.append(artifact)
        self.save(snapshot)
        return artifact

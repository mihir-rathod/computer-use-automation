"""JSON persistence for capability artifacts.

One file per capability, named by capability_id -- discovery (agent/) writes here after a
successful run, and replay (replay/) reads from here. Deliberately just a directory of
files, not a database: there's no query pattern here more complex than "look up by id".
(Versioning and run persistence are the point at which this stops being enough.)
"""
from __future__ import annotations

from pathlib import Path

from artifacts_lib.schema import Artifact

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"


def artifact_path(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> Path:
    return directory / f"{capability_id}.json"


def save_artifact(artifact: Artifact, directory: Path = DEFAULT_ARTIFACTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = artifact_path(artifact.capability_id, directory)
    path.write_text(artifact.model_dump_json(indent=2) + "\n")
    return path


def load_artifact(path: Path) -> Artifact:
    return Artifact.model_validate_json(path.read_text())


def load_artifact_by_id(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> Artifact:
    return load_artifact(artifact_path(capability_id, directory))


def list_artifacts(directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[Artifact]:
    if not directory.exists():
        return []
    return [load_artifact(p) for p in sorted(directory.glob("*.json"))]

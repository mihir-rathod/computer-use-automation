"""Versioned JSON persistence for capability artifacts.

Layout, one directory per capability:

    artifacts/<capability_id>/<version>.json     every version ever saved, never overwritten
    artifacts/<capability_id>/index.json         which version is current, plus a promotion history

Saving never overwrites: a new discovery or repair is a new version. The first version of a
capability becomes current automatically; later ones are candidates until someone promotes them,
so an unreviewed artifact never silently replaces the one production is replaying. `rollback`
returns to whichever version was current before the last promotion.

A flat `artifacts/<capability_id>.json` (the old layout, and what test fixtures use) is still read
transparently; `migrate_flat()` converts a directory in place.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from artifacts_lib.schema import Artifact

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"
INDEX_NAME = "index.json"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class VersionExists(Exception):
    pass


class UnknownVersion(FileNotFoundError):
    pass


def _semver_key(version: str) -> tuple[int, int, int]:
    match = _SEMVER.match(version)
    if not match:
        raise ValueError(f"not a semver: {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def capability_dir(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> Path:
    return directory / capability_id


def _flat_path(capability_id: str, directory: Path) -> Path:
    return directory / f"{capability_id}.json"


def artifact_path(capability_id: str, version: str | None = None, directory: Path = DEFAULT_ARTIFACTS_DIR) -> Path:
    folder = capability_dir(capability_id, directory)
    if folder.is_dir():
        chosen = version or current_version(capability_id, directory)
        if chosen is None:
            raise FileNotFoundError(f"no versions of '{capability_id}' in {folder}")
        return folder / f"{chosen}.json"
    return _flat_path(capability_id, directory)


def _read_index(capability_id: str, directory: Path) -> dict[str, Any]:
    path = capability_dir(capability_id, directory) / INDEX_NAME
    return json.loads(path.read_text()) if path.exists() else {"current": None, "history": []}


def _write_index(capability_id: str, directory: Path, index: dict[str, Any]) -> None:
    (capability_dir(capability_id, directory) / INDEX_NAME).write_text(json.dumps(index, indent=2) + "\n")


def list_versions(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[str]:
    folder = capability_dir(capability_id, directory)
    if folder.is_dir():
        return sorted((p.stem for p in folder.glob("*.json") if p.name != INDEX_NAME), key=_semver_key)
    flat = _flat_path(capability_id, directory)
    return [load_artifact(flat).version] if flat.exists() else []


def current_version(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> str | None:
    versions = list_versions(capability_id, directory)
    if not versions:
        return None
    if not capability_dir(capability_id, directory).is_dir():
        return versions[0]
    current = _read_index(capability_id, directory)["current"]
    return current if current in versions else versions[-1]


def history(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[dict[str, Any]]:
    return _read_index(capability_id, directory)["history"] if capability_dir(capability_id, directory).is_dir() else []


def next_version(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR, bump: str = "patch") -> str:
    versions = list_versions(capability_id, directory)
    if not versions:
        return "1.0.0"
    major, minor, patch = _semver_key(versions[-1])
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def save_artifact(
    artifact: Artifact, directory: Path = DEFAULT_ARTIFACTS_DIR, make_current: bool | None = None,
    by: str = "system", reason: str = "saved",
) -> Path:
    """Writes a new version. `make_current=None` means: current only if this capability has none."""
    folder = capability_dir(artifact.capability_id, directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{artifact.version}.json"
    if path.exists():
        raise VersionExists(f"{artifact.capability_id} {artifact.version} already exists; bump the version")
    path.write_text(artifact.model_dump_json(indent=2) + "\n")
    index = _read_index(artifact.capability_id, directory)
    if make_current is None:
        make_current = index["current"] is None
    if make_current:
        action = "initial" if index["current"] is None else "promote"
        _promote(artifact.capability_id, artifact.version, directory, index, by, reason, action)
    else:
        _write_index(artifact.capability_id, directory, index)
    return path


def _promote(capability_id: str, version: str, directory: Path, index: dict[str, Any], by: str, reason: str, action: str) -> None:
    index["history"].append({
        "action": action, "version": version, "from": index["current"],
        "at": datetime.now(UTC).isoformat(timespec="seconds"), "by": by, "reason": reason,
    })
    index["current"] = version
    _write_index(capability_id, directory, index)


def set_current(capability_id: str, version: str, directory: Path = DEFAULT_ARTIFACTS_DIR, by: str = "system", reason: str = "") -> None:
    if version not in list_versions(capability_id, directory):
        raise UnknownVersion(f"{capability_id} has no version {version}")
    index = _read_index(capability_id, directory)
    if index["current"] != version:
        _promote(capability_id, version, directory, index, by, reason, "promote")


def rollback(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR, by: str = "system", reason: str = "") -> str:
    """Returns to the version that was current before the most recent promotion."""
    index = _read_index(capability_id, directory)
    previous = next((e["from"] for e in reversed(index["history"]) if e["version"] == index["current"] and e["from"]), None)
    if previous is None:
        raise UnknownVersion(f"{capability_id} has nothing to roll back to")
    _promote(capability_id, previous, directory, index, by, reason or "rollback", "rollback")
    return previous


def load_artifact(path: Path) -> Artifact:
    return Artifact.model_validate_json(path.read_text())


def load_artifact_by_id(capability_id: str, directory: Path = DEFAULT_ARTIFACTS_DIR, version: str | None = None) -> Artifact:
    folder = capability_dir(capability_id, directory)
    if folder.is_dir():
        chosen = version or current_version(capability_id, directory)
        path = folder / f"{chosen}.json"
        if chosen is None or not path.exists():
            raise UnknownVersion(f"{capability_id} has no version {version or '(current)'}")
        return load_artifact(path)
    artifact = load_artifact(_flat_path(capability_id, directory))
    if version is not None and artifact.version != version:
        raise UnknownVersion(f"{capability_id} has no version {version}")
    return artifact


def capability_ids(directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[str]:
    if not directory.exists():
        return []
    ids = {p.name for p in directory.iterdir() if p.is_dir() and any(p.glob("*.json"))}
    ids |= {p.stem for p in directory.glob("*.json")}
    return sorted(ids)


def list_artifacts(directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[Artifact]:
    """The current version of every capability."""
    return [load_artifact_by_id(cid, directory) for cid in capability_ids(directory)]


def migrate_flat(directory: Path = DEFAULT_ARTIFACTS_DIR) -> list[str]:
    """Converts `<id>.json` files to the versioned layout in place; returns the ids migrated."""
    migrated = []
    for flat in sorted(directory.glob("*.json")):
        artifact = load_artifact(flat)
        flat.unlink()
        save_artifact(artifact, directory, make_current=True, by="migration", reason="converted from flat layout")
        migrated.append(artifact.capability_id)
    return migrated

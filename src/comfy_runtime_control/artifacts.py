"""Resolve generated artifacts from immutable ComfyUI history records."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from .client import ComfyClient


@dataclass(frozen=True)
class ArtifactRef:
    node_id: str
    channel: str
    filename: str
    subfolder: str
    artifact_type: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def artifacts_from_history(history: dict[str, Any]) -> list[ArtifactRef]:
    outputs = history.get("outputs", {}) if isinstance(history, dict) else {}
    if not isinstance(outputs, dict):
        return []
    found: list[ArtifactRef] = []
    for node_id, output in outputs.items():
        if not isinstance(output, dict):
            continue
        for channel, values in output.items():
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict) or not isinstance(value.get("filename"), str):
                    continue
                found.append(
                    ArtifactRef(
                        node_id=str(node_id),
                        channel=str(channel),
                        filename=value["filename"],
                        subfolder=str(value.get("subfolder", "")),
                        artifact_type=str(value.get("type", "output")),
                    )
                )
    return found


def download_artifact(
    client: ComfyClient,
    artifact: ArtifactRef,
    root: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    subfolder = PurePosixPath(artifact.subfolder.replace("\\", "/"))
    filename = PurePosixPath(artifact.filename.replace("\\", "/"))
    if (
        subfolder.is_absolute()
        or ".." in subfolder.parts
        or filename.is_absolute()
        or ".." in filename.parts
        or len(filename.parts) != 1
    ):
        raise ValueError("artifact path escapes the download root")
    relative = subfolder / filename
    destination = Path(root).joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing download: {destination}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            client.stream_artifact(
                artifact.filename,
                artifact.subfolder,
                artifact.artifact_type,
                temporary,
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        if overwrite:
            os.replace(temporary_path, destination)
        else:
            os.link(temporary_path, destination)
            temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


def unique_physical_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
    """Deduplicate references that point at the same physical ComfyUI file."""
    found: list[ArtifactRef] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        identity = (artifact.artifact_type, artifact.subfolder, artifact.filename)
        if identity in seen:
            continue
        seen.add(identity)
        found.append(artifact)
    return found

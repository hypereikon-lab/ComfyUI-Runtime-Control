"""Resolve generated artifacts from immutable ComfyUI history records."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
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


def download_artifact(client: ComfyClient, artifact: ArtifactRef, root: str | Path) -> Path:
    relative = PurePosixPath(artifact.subfolder.replace("\\", "/")) / PurePosixPath(
        artifact.filename.replace("\\", "/")
    )
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path escapes the download root")
    destination = Path(root).joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        client.view_artifact(artifact.filename, artifact.subfolder, artifact.artifact_type)
    )
    return destination

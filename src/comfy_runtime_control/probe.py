"""Runtime probe and immutable capability manifest."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .canonical import content_hash
from .client import ComfyClient
from .errors import ComfyRuntimeError


PROBE_ROUTES = {
    "features": "/features",
    "system_stats": "/system_stats",
    "object_info": "/object_info",
    "extensions": "/extensions",
    "models": "/models",
    "queue": "/queue",
}


def probe_runtime(client: ComfyClient) -> dict[str, Any]:
    endpoints: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    for name, path in PROBE_ROUTES.items():
        try:
            value = client.get(path)
        except ComfyRuntimeError as exc:
            endpoints[name] = {"available": False, "error": str(exc)[:300]}
            continue
        captured[name] = value
        endpoints[name] = {
            "available": True,
            "content_hash": content_hash(value),
            "item_count": len(value) if hasattr(value, "__len__") else None,
        }

    object_info = captured.get("object_info")
    node_types = sorted(object_info) if isinstance(object_info, dict) else []
    manifest: dict[str, Any] = {
        "schema": "comfy.runtime-manifest/1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": client.safe_runtime_label,
        "client_id": client.client_id,
        "endpoints": endpoints,
        "node_type_count": len(node_types),
        "node_types": node_types,
        "system_stats": captured.get("system_stats"),
        "features": captured.get("features"),
    }
    manifest["manifest_hash"] = content_hash(manifest)
    manifest["_captured_object_info"] = object_info
    return manifest


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove the large schema snapshot when a compact receipt only needs its hash."""

    return {key: value for key, value in manifest.items() if key != "_captured_object_info"}


def validate_runtime_manifest(manifest: Any) -> dict[str, Any]:
    """Return the captured node schemas after verifying the manifest hashes."""

    if not isinstance(manifest, dict) or manifest.get("schema") != "comfy.runtime-manifest/1":
        raise ValueError("invalid runtime manifest")
    object_info = manifest.get("_captured_object_info")
    if not isinstance(object_info, dict):
        raise ValueError("runtime manifest does not contain _captured_object_info")
    expected_manifest_hash = manifest.get("manifest_hash")
    unhashed = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_hash", "_captured_object_info"}
    }
    if expected_manifest_hash != content_hash(unhashed):
        raise ValueError("runtime manifest hash does not match its public fields")
    endpoint = manifest.get("endpoints", {}).get("object_info", {})
    if endpoint.get("content_hash") != content_hash(object_info):
        raise ValueError("runtime manifest object_info hash does not match its snapshot")
    return object_info

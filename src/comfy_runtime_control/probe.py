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


def build_runtime_manifest(
    captured: dict[str, Any],
    *,
    runtime_label: str,
    client_id: str = "",
    captured_at: str | None = None,
    endpoint_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical manifest from bounded endpoint snapshots.

    This supports an authenticated browser handoff without extracting its
    Cloudflare cookie. Only known probe endpoint names are accepted.
    """

    if not isinstance(captured, dict) or not set(captured) <= set(PROBE_ROUTES):
        raise ValueError("captured snapshots contain an unknown probe endpoint")
    if not isinstance(runtime_label, str) or not runtime_label:
        raise ValueError("runtime_label is required")
    errors = endpoint_errors or {}
    if not isinstance(errors, dict) or not set(errors) <= set(PROBE_ROUTES):
        raise ValueError("endpoint_errors contain an unknown probe endpoint")
    endpoints: dict[str, Any] = {}
    for name in PROBE_ROUTES:
        if name in captured:
            value = captured[name]
            endpoints[name] = {
                "available": True,
                "content_hash": content_hash(value),
                "item_count": len(value) if hasattr(value, "__len__") else None,
            }
        else:
            endpoints[name] = {
                "available": False,
                "error": str(errors.get(name, "not captured"))[:300],
            }

    object_info = captured.get("object_info")
    node_types = sorted(object_info) if isinstance(object_info, dict) else []
    manifest: dict[str, Any] = {
        "schema": "comfy.runtime-manifest/1",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "runtime": runtime_label,
        "client_id": client_id,
        "endpoints": endpoints,
        "node_type_count": len(node_types),
        "node_types": node_types,
        "system_stats": captured.get("system_stats"),
        "features": captured.get("features"),
    }
    manifest["manifest_hash"] = content_hash(manifest)
    for name, value in captured.items():
        manifest[f"_captured_{name}"] = value
    return manifest


def probe_runtime(client: ComfyClient) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    endpoint_errors: dict[str, str] = {}
    for name, path in PROBE_ROUTES.items():
        try:
            value = client.get(path)
        except ComfyRuntimeError as exc:
            endpoint_errors[name] = str(exc)
            continue
        captured[name] = value
    return build_runtime_manifest(
        captured,
        runtime_label=client.safe_runtime_label,
        client_id=client.client_id,
        endpoint_errors=endpoint_errors,
    )


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Remove the large schema snapshot when a compact receipt only needs its hash."""

    return {key: value for key, value in manifest.items() if not key.startswith("_captured_")}


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
        if key != "manifest_hash" and not key.startswith("_captured_")
    }
    if expected_manifest_hash != content_hash(unhashed):
        raise ValueError("runtime manifest hash does not match its public fields")
    endpoints = manifest.get("endpoints", {})
    for key, value in manifest.items():
        if not key.startswith("_captured_"):
            continue
        name = key.removeprefix("_captured_")
        endpoint = endpoints.get(name, {}) if isinstance(endpoints, dict) else {}
        if endpoint.get("content_hash") != content_hash(value):
            raise ValueError(f"runtime manifest {name} hash does not match its snapshot")
    return object_info

"""Narrow Manager adapter with exact-target mutation guards."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .client import ComfyClient
from .errors import ComfyRuntimeError, MutationGuardError


@dataclass(frozen=True)
class MutationPlan:
    operation: str
    target: str
    route: str
    payload: dict[str, Any]


def plan_custom_node_update(
    target: str,
    *,
    version: str = "unknown",
    source_url: str | None = None,
) -> MutationPlan:
    normalized = target.strip()
    if not normalized or normalized in {"*", "all", "update-all"}:
        raise ValueError("an exact custom-node identifier is required")
    normalized_version = version.strip()
    if not normalized_version:
        raise ValueError("version cannot be empty")
    payload: dict[str, Any] = {
        "id": normalized,
        "ui_id": normalized,
        "version": normalized_version,
    }
    if normalized_version == "unknown":
        if source_url is None:
            raise ValueError("source_url is required for an unknown Git package")
        _validate_git_url(source_url)
        payload["files"] = [source_url]
    return MutationPlan(
        operation="custom-node-update",
        target=normalized,
        route="/manager/queue/update",
        payload=payload,
    )


def apply_mutation(client: ComfyClient, plan: MutationPlan, *, confirmation: str) -> Any:
    if confirmation != plan.target:
        raise MutationGuardError("confirmation must exactly match the planned target")
    client.post("/manager/queue/reset", {})
    client.post(plan.route, plan.payload)
    return client.post("/manager/queue/start", {})


def manager_queue_status(client: ComfyClient) -> Any:
    return client.get("/manager/queue/status")


def reboot_comfy(client: ComfyClient, *, confirmation: str) -> Any:
    if confirmation != "restart-comfy-process":
        raise MutationGuardError("confirmation must be restart-comfy-process")
    return client.post("/manager/reboot", {})


def workspace_capabilities(client: ComfyClient) -> Any:
    """Probe the optional bounded workspace extension; this is read-only."""

    return client.get("/workspace-control/capabilities")


def install_git_url(client: ComfyClient, source_url: str, *, confirmation: str) -> str:
    """Install one exact public Git repository through Manager's guarded route."""

    normalized = source_url.rstrip("/")
    _validate_git_url(normalized)
    if confirmation.rstrip("/") != normalized:
        raise MutationGuardError("confirmation must exactly match the Git URL")
    try:
        _, body = client.request_bytes(
            "POST",
            "/customnode/install/git_url",
            json_body={"url": normalized},
        )
    except ComfyRuntimeError as exc:
        # Manager 3.32 documents a text/plain body, while the lab's current
        # build rejects that shape and requires {"url": ...}. A 400 is the
        # only safe compatibility retry because the first request was rejected
        # before an installation transaction could begin.
        if "unexpected HTTP 400" not in str(exc):
            raise
        _, body = client.request_bytes(
            "POST",
            "/customnode/install/git_url",
            data=normalized.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    if not body:
        return "installed"
    decoded = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return decoded
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("status", "message", "result"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validate_git_url(value: str) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        raise ValueError("only an exact public HTTPS GitHub repository URL is allowed")

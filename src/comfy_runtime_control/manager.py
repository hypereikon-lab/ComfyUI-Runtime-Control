"""Narrow Manager adapter with exact-target mutation guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import ComfyClient
from .errors import MutationGuardError


@dataclass(frozen=True)
class MutationPlan:
    operation: str
    target: str
    route: str
    payload: dict[str, Any]


def plan_custom_node_update(target: str) -> MutationPlan:
    normalized = target.strip()
    if not normalized or normalized in {"*", "all", "update-all"}:
        raise ValueError("an exact custom-node identifier is required")
    return MutationPlan(
        operation="custom-node-update",
        target=normalized,
        route="/manager/queue/update",
        payload={"id": normalized},
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

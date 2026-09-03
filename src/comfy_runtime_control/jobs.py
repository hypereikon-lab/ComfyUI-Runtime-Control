"""API graph submission, observation, cancellation, and completion."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from .client import ComfyClient
from .errors import ComfyRuntimeError, JobExecutionError


@dataclass(frozen=True)
class SubmittedJob:
    prompt_id: str
    number: int | None
    node_errors: dict[str, Any]


def submit_graph(
    client: ComfyClient,
    graph: dict[str, Any],
    *,
    extra_data: dict[str, Any] | None = None,
) -> SubmittedJob:
    body: dict[str, Any] = {"prompt": graph, "client_id": client.client_id}
    if extra_data:
        body["extra_data"] = extra_data
    response = client.post("/prompt", body)
    if not isinstance(response, dict) or not response.get("prompt_id"):
        raise ComfyRuntimeError("/prompt did not return a prompt_id")
    return SubmittedJob(
        prompt_id=str(response["prompt_id"]),
        number=response.get("number") if isinstance(response.get("number"), int) else None,
        node_errors=response.get("node_errors", {}) if isinstance(response.get("node_errors"), dict) else {},
    )


def job_history(client: ComfyClient, prompt_id: str) -> dict[str, Any] | None:
    response = client.get(f"/history/{prompt_id}")
    if not isinstance(response, dict):
        raise ComfyRuntimeError("history response must be an object")
    value = response.get(prompt_id)
    return value if isinstance(value, dict) else None


def wait_for_job(
    client: ComfyClient,
    prompt_id: str,
    *,
    timeout: float = 3600.0,
    interval: float = 5.0,
) -> dict[str, Any]:
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = job_history(client, prompt_id)
        if history is not None:
            status = history.get("status", {})
            if isinstance(status, dict) and status.get("completed") is False:
                messages = status.get("messages", [])
                raise JobExecutionError(f"job {prompt_id} failed: {messages!r}")
            return history
        time.sleep(min(interval, max(0.01, deadline - time.monotonic())))
    raise TimeoutError(f"job {prompt_id} did not finish within {timeout:g} seconds")


def queue_state(client: ComfyClient) -> dict[str, Any]:
    value = client.get("/queue")
    if not isinstance(value, dict):
        raise ComfyRuntimeError("queue response must be an object")
    return value


def interrupt_current(client: ComfyClient) -> Any:
    return client.post("/interrupt", {})


def delete_queued(client: ComfyClient, prompt_ids: list[str]) -> Any:
    if not prompt_ids or any(not value for value in prompt_ids):
        raise ValueError("one or more exact prompt ids are required")
    return client.post("/queue", {"delete": prompt_ids})

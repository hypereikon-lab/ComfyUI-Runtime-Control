"""Durable, read-only availability observations for a shared ComfyUI host."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .canonical import content_hash
from .client import ComfyClient


POLICY_SCHEMA = "comfy.availability-policy/1"
STATE_SCHEMA = "comfy.availability-state/1"
REPORT_SCHEMA = "comfy.availability-report/1"


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def validate_availability_policy(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "id",
        "device_name_contains",
        "minimum_free_ram_bytes",
        "minimum_free_vram_bytes",
        "require_queue_idle",
        "stable_for_seconds",
        "maximum_sample_gap_seconds",
    }
    if not isinstance(value, dict) or value.get("schema") != POLICY_SCHEMA:
        raise ValueError("invalid availability policy")
    if set(value) != required:
        raise ValueError("availability policy fields are incomplete or unexpected")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError("availability policy id is required")
    if not isinstance(value["device_name_contains"], str):
        raise ValueError("device_name_contains must be a string")
    _non_negative_int(value["minimum_free_ram_bytes"], "minimum_free_ram_bytes")
    _non_negative_int(value["minimum_free_vram_bytes"], "minimum_free_vram_bytes")
    _non_negative_int(value["stable_for_seconds"], "stable_for_seconds")
    maximum_gap = _non_negative_int(
        value["maximum_sample_gap_seconds"], "maximum_sample_gap_seconds"
    )
    if maximum_gap == 0:
        raise ValueError("maximum_sample_gap_seconds must be positive")
    if not isinstance(value["require_queue_idle"], bool):
        raise ValueError("require_queue_idle must be boolean")
    return value


def _parse_timestamp(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp or null") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_state(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    policy_hash = content_hash(policy)
    if not path.exists():
        return {
            "schema": STATE_SCHEMA,
            "policy_id": policy["id"],
            "policy_hash": policy_hash,
            "sample_count": 0,
            "last_observed_at": None,
            "qualifying_since": None,
            "ready_since": None,
            "last_qualifies_now": False,
            "last_report": None,
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema",
        "policy_id",
        "policy_hash",
        "sample_count",
        "last_observed_at",
        "qualifying_since",
        "ready_since",
        "last_qualifies_now",
        "last_report",
    }
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise ValueError("invalid availability state")
    if set(value) != required:
        raise ValueError("availability state fields are incomplete or unexpected")
    if value["policy_id"] != policy["id"] or value["policy_hash"] != policy_hash:
        raise ValueError("availability state belongs to a different policy")
    _non_negative_int(value["sample_count"], "sample_count")
    _parse_timestamp(value["last_observed_at"], "last_observed_at")
    _parse_timestamp(value["qualifying_since"], "qualifying_since")
    _parse_timestamp(value["ready_since"], "ready_since")
    if not isinstance(value["last_qualifies_now"], bool):
        raise ValueError("last_qualifies_now must be boolean")
    if value["last_report"] is not None and not isinstance(value["last_report"], dict):
        raise ValueError("last_report must be an object or null")
    return value


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _select_device(devices: Any, name_contains: str) -> dict[str, Any] | None:
    if not isinstance(devices, list):
        return None
    candidates = [item for item in devices if isinstance(item, dict)]
    needle = name_contains.casefold()
    if needle:
        candidates = [
            item for item in candidates if needle in str(item.get("name", "")).casefold()
        ]
    return max(
        candidates,
        key=lambda item: item.get("vram_total")
        if isinstance(item.get("vram_total"), int)
        else -1,
        default=None,
    )


def _queue_idle(queue: Any) -> tuple[bool | None, int | None, int | None]:
    if not isinstance(queue, dict):
        return None, None, None
    running = queue.get("queue_running")
    pending = queue.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return None, None, None
    return not running and not pending, len(running), len(pending)


def observe_availability(
    client: ComfyClient,
    policy: Any,
    *,
    state_path: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Observe one sample and durably advance its stability window.

    This function is deliberately read-only with respect to ComfyUI. It does
    not queue a graph, free models, interrupt work, or launch a command.
    """

    policy = validate_availability_policy(policy)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state_file = Path(state_path)
    state = _load_state(state_file, policy)
    previous_ready = state["ready_since"] is not None

    system_stats = client.get("/system_stats")
    queue = client.get("/queue")
    if not isinstance(system_stats, dict):
        raise ValueError("system_stats response must be an object")
    system = system_stats.get("system")
    system = system if isinstance(system, dict) else {}
    device = _select_device(system_stats.get("devices"), policy["device_name_contains"])
    ram_free = system.get("ram_free")
    vram_free = device.get("vram_free") if device else None
    queue_idle, queue_running, queue_pending = _queue_idle(queue)

    torch_reserved = device.get("torch_vram_total") if device else None
    torch_reusable = device.get("torch_vram_free") if device else None
    torch_active = None
    if isinstance(torch_reserved, int) and isinstance(torch_reusable, int):
        torch_active = max(0, torch_reserved - torch_reusable)

    checks = {
        "device_matched": device is not None,
        "minimum_free_ram": isinstance(ram_free, int)
        and ram_free >= policy["minimum_free_ram_bytes"],
        "minimum_free_vram": isinstance(vram_free, int)
        and vram_free >= policy["minimum_free_vram_bytes"],
        "queue_idle": queue_idle is True if policy["require_queue_idle"] else True,
    }
    qualifies_now = all(checks.values())

    last_observed = _parse_timestamp(state["last_observed_at"], "last_observed_at")
    if last_observed is not None and observed_at < last_observed:
        raise ValueError("observation timestamp precedes the persisted state")
    gap_seconds = (
        (observed_at - last_observed).total_seconds() if last_observed is not None else None
    )
    continuity_preserved = (
        state["last_qualifies_now"]
        and gap_seconds is not None
        and gap_seconds <= policy["maximum_sample_gap_seconds"]
    )

    if qualifies_now:
        qualifying_since = (
            _parse_timestamp(state["qualifying_since"], "qualifying_since")
            if continuity_preserved
            else observed_at
        )
        if qualifying_since is None:
            qualifying_since = observed_at
        stable_seconds = max(0, int((observed_at - qualifying_since).total_seconds()))
        ready = stable_seconds >= policy["stable_for_seconds"]
        if ready:
            ready_since = (
                _parse_timestamp(state["ready_since"], "ready_since") or observed_at
            )
        else:
            ready_since = None
    else:
        qualifying_since = None
        ready_since = None
        stable_seconds = 0
        ready = False

    transition = None
    if ready and not previous_ready:
        transition = "became-ready"
    elif previous_ready and not ready:
        transition = "became-unavailable"

    report = {
        "schema": REPORT_SCHEMA,
        "policy_id": policy["id"],
        "policy_hash": content_hash(policy),
        "observed_at": _timestamp(observed_at),
        "qualifies_now": qualifies_now,
        "ready": ready,
        "transition": transition,
        "stable_seconds": stable_seconds,
        "stable_required_seconds": policy["stable_for_seconds"],
        "qualifying_since": _timestamp(qualifying_since) if qualifying_since else None,
        "ready_since": _timestamp(ready_since) if ready_since else None,
        "sample_gap_seconds": int(gap_seconds) if gap_seconds is not None else None,
        "continuity_preserved": continuity_preserved,
        "checks": checks,
        "observation": {
            "ram_total_bytes": system.get("ram_total"),
            "ram_free_bytes": ram_free,
            "device_name": device.get("name") if device else None,
            "vram_total_bytes": device.get("vram_total") if device else None,
            "vram_free_bytes": vram_free,
            "comfy_torch_reserved_vram_bytes": torch_reserved,
            "comfy_torch_reusable_vram_bytes": torch_reusable,
            "comfy_torch_active_vram_bytes": torch_active,
            "queue_running": queue_running,
            "queue_pending": queue_pending,
        },
        "thresholds": {
            "minimum_free_ram_bytes": policy["minimum_free_ram_bytes"],
            "minimum_free_vram_bytes": policy["minimum_free_vram_bytes"],
            "require_queue_idle": policy["require_queue_idle"],
            "maximum_sample_gap_seconds": policy["maximum_sample_gap_seconds"],
        },
        "state_path": str(state_file),
    }
    next_state = {
        "schema": STATE_SCHEMA,
        "policy_id": policy["id"],
        "policy_hash": content_hash(policy),
        "sample_count": state["sample_count"] + 1,
        "last_observed_at": report["observed_at"],
        "qualifying_since": report["qualifying_since"],
        "ready_since": report["ready_since"],
        "last_qualifies_now": qualifies_now,
        "last_report": report,
    }
    _write_state(state_file, next_state)
    return report

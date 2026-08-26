"""Read-only compatibility gates over one captured ComfyUI runtime manifest."""

from __future__ import annotations

from typing import Any, Iterable

from .probe import validate_runtime_manifest


REQUIREMENTS_SCHEMA = "comfy.runtime-requirements/1"
REPORT_SCHEMA = "comfy.runtime-readiness/1"


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must not contain duplicates")
    return value


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _model_available(filename: str, values: set[str]) -> bool:
    normalized = filename.replace("\\", "/")
    for value in values:
        candidate = value.replace("\\", "/")
        if candidate == normalized or candidate.endswith(f"/{normalized}"):
            return True
    return False


def _positive_int_or_zero(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def validate_runtime_requirements(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != REQUIREMENTS_SCHEMA:
        raise ValueError("invalid runtime requirements")
    required = {
        "schema",
        "id",
        "required_endpoints",
        "required_node_types",
        "node_type_groups",
        "required_models",
        "hardware",
        "require_queue_idle",
        "manual_checks",
    }
    if set(value) != required:
        raise ValueError("runtime requirements fields are incomplete or unexpected")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError("runtime requirements id is required")
    _string_list(value["required_endpoints"], "required_endpoints")
    _string_list(value["required_node_types"], "required_node_types")
    _string_list(value["required_models"], "required_models")
    _string_list(value["manual_checks"], "manual_checks")
    groups = value["node_type_groups"]
    if not isinstance(groups, list):
        raise ValueError("node_type_groups must be a list")
    group_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"id", "any_of"}:
            raise ValueError("node type group must contain exactly id and any_of")
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id or group_id in group_ids:
            raise ValueError("node type group ids must be unique non-empty strings")
        group_ids.add(group_id)
        choices = _string_list(group.get("any_of"), f"node_type_groups[{group_id}].any_of")
        if not choices:
            raise ValueError(f"node type group {group_id} requires at least one choice")
    hardware = value["hardware"]
    if not isinstance(hardware, dict) or set(hardware) != {
        "minimum_total_ram_bytes",
        "minimum_total_vram_bytes",
        "device_name_contains",
    }:
        raise ValueError("hardware requirements are incomplete or unexpected")
    _positive_int_or_zero(hardware["minimum_total_ram_bytes"], "minimum_total_ram_bytes")
    _positive_int_or_zero(hardware["minimum_total_vram_bytes"], "minimum_total_vram_bytes")
    if not isinstance(hardware["device_name_contains"], str):
        raise ValueError("device_name_contains must be a string")
    if not isinstance(value["require_queue_idle"], bool):
        raise ValueError("require_queue_idle must be boolean")
    return value


def _queue_is_idle(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    running = value.get("queue_running")
    pending = value.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return None
    return not running and not pending


def evaluate_runtime_requirements(
    requirements: Any, manifest: Any
) -> dict[str, Any]:
    requirements = validate_runtime_requirements(requirements)
    object_info = validate_runtime_manifest(manifest)
    endpoints = manifest.get("endpoints", {})
    missing_endpoints = sorted(
        name
        for name in requirements["required_endpoints"]
        if not isinstance(endpoints.get(name), dict) or endpoints[name].get("available") is not True
    )

    available_node_types = set(object_info)
    missing_node_types = sorted(set(requirements["required_node_types"]) - available_node_types)
    missing_node_type_groups = [
        {
            "id": group["id"],
            "any_of": group["any_of"],
        }
        for group in requirements["node_type_groups"]
        if not available_node_types.intersection(group["any_of"])
    ]

    model_values = set(_walk_strings(object_info))
    model_values.update(_walk_strings(manifest.get("_captured_models")))
    missing_models = sorted(
        filename
        for filename in requirements["required_models"]
        if not _model_available(filename, model_values)
    )

    system_stats = manifest.get("system_stats")
    system = system_stats.get("system", {}) if isinstance(system_stats, dict) else {}
    devices = system_stats.get("devices", []) if isinstance(system_stats, dict) else []
    total_ram = system.get("ram_total") if isinstance(system, dict) else None
    device_records = [item for item in devices if isinstance(item, dict)] if isinstance(devices, list) else []
    total_vram = max(
        (item.get("vram_total") for item in device_records if isinstance(item.get("vram_total"), int)),
        default=None,
    )
    device_names = [str(item.get("name", "")) for item in device_records]
    hardware = requirements["hardware"]
    hardware_failures: list[str] = []
    if not isinstance(total_ram, int) or total_ram < hardware["minimum_total_ram_bytes"]:
        hardware_failures.append("minimum_total_ram_bytes")
    if not isinstance(total_vram, int) or total_vram < hardware["minimum_total_vram_bytes"]:
        hardware_failures.append("minimum_total_vram_bytes")
    required_device = hardware["device_name_contains"].casefold()
    if required_device and not any(required_device in name.casefold() for name in device_names):
        hardware_failures.append("device_name_contains")

    queue_idle = _queue_is_idle(manifest.get("_captured_queue"))
    queue_failure = requirements["require_queue_idle"] and queue_idle is not True
    blocking = bool(
        missing_endpoints
        or missing_node_types
        or missing_node_type_groups
        or missing_models
        or hardware_failures
        or queue_failure
    )
    return {
        "schema": REPORT_SCHEMA,
        "requirements_id": requirements["id"],
        "runtime_manifest_hash": manifest.get("manifest_hash"),
        "ready": not blocking,
        "checks": {
            "endpoints": {
                "required": requirements["required_endpoints"],
                "missing": missing_endpoints,
            },
            "node_types": {
                "required_count": len(requirements["required_node_types"]),
                "missing": missing_node_types,
                "missing_groups": missing_node_type_groups,
            },
            "models": {
                "required": requirements["required_models"],
                "missing": missing_models,
            },
            "hardware": {
                "observed_total_ram_bytes": total_ram,
                "observed_total_vram_bytes": total_vram,
                "observed_device_names": device_names,
                "failures": hardware_failures,
            },
            "queue": {
                "required_idle": requirements["require_queue_idle"],
                "observed_idle": queue_idle,
                "failure": queue_failure,
            },
        },
        "manual_checks": requirements["manual_checks"],
    }

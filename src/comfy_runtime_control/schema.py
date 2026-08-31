"""Validation and dependency planning for ComfyUI API prompt graphs."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    node_id: str | None
    input_name: str | None
    message: str


def _required_inputs(node_schema: dict[str, Any]) -> dict[str, Any]:
    inputs = node_schema.get("input", {}) if isinstance(node_schema, dict) else {}
    required = inputs.get("required", {}) if isinstance(inputs, dict) else {}
    return required if isinstance(required, dict) else {}


def _all_inputs(node_schema: dict[str, Any]) -> dict[str, Any]:
    inputs = node_schema.get("input", {}) if isinstance(node_schema, dict) else {}
    result: dict[str, Any] = {}
    if isinstance(inputs, dict):
        for group in ("required", "optional", "hidden"):
            values = inputs.get(group, {})
            if isinstance(values, dict):
                result.update(values)
    return result


def _is_dynamic_combo_child(name: str, declared: dict[str, Any]) -> bool:
    """Recognize frontend-flattened fields such as ``format.codec``.

    Comfy's dynamic-combo widgets serialize selected nested inputs beside the
    root input. They are executable prompt fields even though `/object_info`
    declares only the root `COMFY_DYNAMICCOMBO_V3` input.
    """

    root, separator, _child = name.partition(".")
    if not separator or root not in declared:
        return False
    declaration = declared[root]
    return (
        isinstance(declaration, (list, tuple))
        and bool(declaration)
        and declaration[0] == "COMFY_DYNAMICCOMBO_V3"
    )


def validate_api_graph(graph: Any, object_info: Any) -> dict[str, Any]:
    issues: list[ValidationIssue] = []
    referenced_types: set[str] = set()
    if not isinstance(graph, dict):
        issues.append(ValidationIssue("error", "graph_type", None, None, "API graph must be an object"))
        return _report(issues, referenced_types)
    if not isinstance(object_info, dict):
        issues.append(
            ValidationIssue("error", "schema_type", None, None, "object_info must be an object")
        )
        return _report(issues, referenced_types)

    node_ids = {str(value) for value in graph}
    for raw_id, node in graph.items():
        node_id = str(raw_id)
        if not isinstance(node, dict):
            issues.append(ValidationIssue("error", "node_type", node_id, None, "node must be an object"))
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            issues.append(ValidationIssue("error", "class_type", node_id, None, "missing class_type"))
            continue
        referenced_types.add(class_type)
        schema = object_info.get(class_type)
        if not isinstance(schema, dict):
            issues.append(
                ValidationIssue("error", "unknown_node", node_id, None, f"unknown node type {class_type}")
            )
            continue
        supplied = node.get("inputs", {})
        if not isinstance(supplied, dict):
            issues.append(ValidationIssue("error", "inputs_type", node_id, None, "inputs must be an object"))
            continue
        declared = _all_inputs(schema)
        for name in _required_inputs(schema):
            if name not in supplied:
                issues.append(
                    ValidationIssue("error", "missing_input", node_id, name, "required input is missing")
                )
        for name, value in supplied.items():
            if name not in declared and not _is_dynamic_combo_child(name, declared):
                issues.append(
                    ValidationIssue("warning", "unknown_input", node_id, name, "input is absent from live schema")
                )
                continue
            if name not in declared:
                continue
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
                if str(value[0]) not in node_ids:
                    issues.append(
                        ValidationIssue("error", "missing_link_source", node_id, name, f"link source {value[0]} is absent")
                    )
                if not isinstance(value[1], int) or value[1] < 0:
                    issues.append(
                        ValidationIssue("error", "invalid_link_slot", node_id, name, "link output slot must be a non-negative integer")
                    )
                continue
            declaration = declared[name]
            choices = declaration[0] if isinstance(declaration, (list, tuple)) and declaration else None
            if isinstance(choices, list) and choices and value not in choices:
                issues.append(
                    ValidationIssue("error", "enum_value", node_id, name, f"value {value!r} is not in the live choices")
                )
    return _report(issues, referenced_types)


def _report(issues: list[ValidationIssue], referenced_types: set[str]) -> dict[str, Any]:
    serialized = [asdict(issue) for issue in issues]
    errors = sum(issue.severity == "error" for issue in issues)
    return {
        "schema": "comfy.api-graph-validation/1",
        "valid": errors == 0,
        "error_count": errors,
        "warning_count": sum(issue.severity == "warning" for issue in issues),
        "referenced_node_types": sorted(referenced_types),
        "issues": serialized,
    }


def dependency_plan(graph: dict[str, Any], object_info: dict[str, Any]) -> dict[str, Any]:
    validation = validate_api_graph(graph, object_info)
    required = validation["referenced_node_types"]
    available = sorted(name for name in required if name in object_info)
    missing = sorted(name for name in required if name not in object_info)
    return {
        "schema": "comfy.dependency-plan/1",
        "required_node_types": required,
        "available_node_types": available,
        "missing_node_types": missing,
        "ready": not missing and validation["valid"],
        "validation": validation,
    }

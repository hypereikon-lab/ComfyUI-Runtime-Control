"""Turn one exact browser export into a reproducible UI/API draft pair."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .canonical import content_hash
from .compiler import compile_api_template
from .errors import GraphValidationError
from .probe import validate_runtime_manifest


WORKSPACE_EXPORT_SCHEMAS = frozenset(
    {"comfy.workspace-export/1", "comfy.workspace-export/2"}
)
PARAMETERIZATION_SCHEMA = "comfy.api-parameterization/1"
MATERIALIZATION_DRAFT_SCHEMA = "comfy.materialization-draft/1"
OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
BINDING_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MaterializedDraft:
    ui_graph: dict[str, Any]
    api_template: dict[str, Any]
    bindings: dict[str, Any]
    manifest: dict[str, Any]


def write_materialized_draft(
    directory: str | Path,
    draft: MaterializedDraft,
    *,
    overwrite: bool = False,
) -> dict[str, str]:
    """Write all four draft products without silently replacing existing files."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    products = draft.manifest["products"]
    stem = f"{draft.manifest['operation']['id']}.{draft.manifest['variant']}"
    values = {
        products["ui_graph"]: draft.ui_graph,
        products["api_template"]: draft.api_template,
        products["bindings"]: draft.bindings,
        f"{stem}.materialization.json": draft.manifest,
    }
    paths = {name: root / name for name in values}
    existing = sorted(str(path) for path in paths.values() if path.exists())
    if existing and not overwrite:
        raise FileExistsError(f"refusing to replace existing draft files: {existing}")
    for name, value in values.items():
        paths[name].write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {name: str(path) for name, path in paths.items()}


def _decode_pointer(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise GraphValidationError(f"invalid JSON pointer {pointer!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _target(graph: dict[str, Any], pointer: str) -> tuple[Any, str]:
    parts = _decode_pointer(pointer)
    if len(parts) < 3 or parts[1] != "inputs":
        raise GraphValidationError(
            f"{pointer}: parameterization is restricted to /<node_id>/inputs/..."
        )
    if parts[0] not in graph:
        raise GraphValidationError(f"{pointer}: node does not exist")
    current: Any = graph
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise GraphValidationError(f"{pointer}: path does not exist")
    leaf = parts[-1]
    if isinstance(current, dict) and leaf in current:
        return current, leaf
    if isinstance(current, list) and leaf.isdigit() and int(leaf) < len(current):
        return current, leaf
    raise GraphValidationError(f"{pointer}: path does not exist")


def _read_target(container: Any, leaf: str) -> Any:
    return container[int(leaf)] if isinstance(container, list) else container[leaf]


def _write_target(container: Any, leaf: str, value: Any) -> None:
    if isinstance(container, list):
        container[int(leaf)] = value
    else:
        container[leaf] = value


def _looks_like_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


def _operation_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("operation_ref must be an object")
    operation_id = value.get("id")
    version = value.get("version")
    contract_hash = value.get("contract_hash")
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_ref.id must be a semantic operation id")
    if not isinstance(version, int) or version < 1:
        raise ValueError("operation_ref.version must be a positive integer")
    if not isinstance(contract_hash, str) or not SHA256.fullmatch(contract_hash):
        raise ValueError("operation_ref.contract_hash must be lowercase SHA-256")
    return {"id": operation_id, "version": version, "contract_hash": contract_hash}


def _validate_export(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema") not in WORKSPACE_EXPORT_SCHEMAS:
        raise GraphValidationError("invalid Workspace Control export")
    ui_graph = value.get("uiGraph")
    api_graph = value.get("apiGraph")
    if not isinstance(ui_graph, dict) or not ui_graph:
        raise GraphValidationError("workspace export requires a non-empty uiGraph")
    if not isinstance(api_graph, dict) or not api_graph:
        raise GraphValidationError("workspace export requires a non-empty apiGraph")
    expected = {
        "uiGraphSignature": content_hash(ui_graph),
        "apiGraphSignature": content_hash(api_graph),
    }
    for field, actual in expected.items():
        if value.get(field) != actual:
            raise GraphValidationError(f"workspace export {field} does not match its graph")
    return ui_graph, api_graph


def parameterize_api_graph(
    api_graph: dict[str, Any], parameterization: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace guarded literal input values with exact binding markers."""

    if (
        not isinstance(parameterization, Mapping)
        or parameterization.get("schema") != PARAMETERIZATION_SCHEMA
    ):
        raise GraphValidationError("invalid API parameterization")
    parameters = parameterization.get("parameters")
    if not isinstance(parameters, list) or not parameters:
        raise GraphValidationError("parameterization requires one or more parameters")

    template = deepcopy(api_graph)
    bindings: dict[str, Any] = {}
    seen_pointers: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, dict) or set(parameter) != {"name", "pointers", "expected"}:
            raise GraphValidationError("each parameter requires exactly name, pointers, and expected")
        name = parameter["name"]
        pointers = parameter["pointers"]
        expected = parameter["expected"]
        if not isinstance(name, str) or not BINDING_NAME.fullmatch(name):
            raise GraphValidationError(f"invalid binding name {name!r}")
        if name in bindings:
            raise GraphValidationError(f"duplicate binding name {name!r}")
        if not isinstance(pointers, list) or not pointers:
            raise GraphValidationError(f"binding {name!r} requires one or more pointers")
        if _looks_like_link(expected):
            raise GraphValidationError(f"binding {name!r} cannot parameterize a graph link")
        for pointer in pointers:
            if pointer in seen_pointers:
                raise GraphValidationError(f"duplicate parameter pointer {pointer!r}")
            container, leaf = _target(template, pointer)
            actual = _read_target(container, leaf)
            if content_hash(actual) != content_hash(expected):
                raise GraphValidationError(f"{pointer}: captured value does not match expected")
            if _looks_like_link(actual):
                raise GraphValidationError(f"{pointer}: graph links cannot be parameterized")
            _write_target(container, leaf, {"$binding": name})
            seen_pointers.add(pointer)
        bindings[name] = deepcopy(expected)
    return template, bindings


def materialize_workspace_export(
    workspace_export: Mapping[str, Any],
    parameterization: Mapping[str, Any],
    operation_ref: Mapping[str, Any],
    variant: str,
    *,
    runtime_manifest: dict[str, Any] | None = None,
) -> MaterializedDraft:
    """Create a draft pair and prove that its bindings reconstruct the export."""

    ui_graph, api_graph = _validate_export(workspace_export)
    operation = _operation_reference(operation_ref)
    if not isinstance(variant, str) or not BINDING_NAME.fullmatch(variant.replace("-", "_")):
        raise ValueError("variant must be a non-empty lowercase identifier")

    object_info = validate_runtime_manifest(runtime_manifest) if runtime_manifest is not None else None
    api_template, bindings = parameterize_api_graph(api_graph, parameterization)
    compiled = compile_api_template(
        api_template,
        bindings,
        object_info=object_info,
        reject_unused_bindings=True,
    )
    if compiled.graph_hash != content_hash(api_graph):
        raise GraphValidationError("template round-trip did not reconstruct the exported API graph")

    stem = f"{operation['id']}.{variant}"
    manifest = {
        "schema": MATERIALIZATION_DRAFT_SCHEMA,
        "state": "schema-validated-draft" if runtime_manifest is not None else "offline-draft",
        "operation": operation,
        "variant": variant,
        "source": {
            "schema": workspace_export.get("schema"),
            "captured_at": workspace_export.get("capturedAt"),
            "active_path": workspace_export.get("activePath"),
            "workspace_control_version": workspace_export.get("workspaceControlVersion"),
            "ui_graph_hash": content_hash(ui_graph),
            "api_graph_hash": content_hash(api_graph),
        },
        "products": {
            "ui_graph": f"{stem}.ui.json",
            "api_template": f"{stem}.api.template.json",
            "bindings": f"{stem}.bindings.json",
            "ui_graph_hash": content_hash(ui_graph),
            "api_template_hash": content_hash(api_template),
            "bindings_hash": content_hash(bindings),
        },
        "round_trip": {
            "valid": True,
            "compiled_api_graph_hash": compiled.graph_hash,
            "used_bindings": list(compiled.used_bindings),
        },
        "schema_validation": compiled.validation,
        "runtime_manifest": (
            {
                "manifest_hash": runtime_manifest["manifest_hash"],
                "captured_at": runtime_manifest.get("captured_at"),
                "runtime": runtime_manifest.get("runtime"),
                "object_info_hash": runtime_manifest["endpoints"]["object_info"]["content_hash"],
            }
            if runtime_manifest is not None
            else None
        ),
        "promotion_gate": "requires-live-review",
    }
    return MaterializedDraft(
        ui_graph=deepcopy(ui_graph),
        api_template=api_template,
        bindings=bindings,
        manifest=manifest,
    )

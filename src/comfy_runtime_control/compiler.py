"""Deterministic API-graph template materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import content_hash
from .errors import GraphValidationError
from .schema import validate_api_graph


@dataclass(frozen=True)
class CompiledGraph:
    graph: dict[str, Any]
    graph_hash: str
    used_bindings: tuple[str, ...]
    validation: dict[str, Any] | None


def _compile(value: Any, bindings: dict[str, Any], used: set[str], path: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$binding"}:
            name = value["$binding"]
            if not isinstance(name, str) or not name:
                raise GraphValidationError(f"{path}: $binding must name a value")
            if name not in bindings:
                raise GraphValidationError(f"{path}: missing binding {name!r}")
            used.add(name)
            return bindings[name]
        if "$binding" in value:
            raise GraphValidationError(f"{path}: $binding objects cannot contain other keys")
        return {
            str(key): _compile(child, bindings, used, f"{path}.{key}")
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_compile(child, bindings, used, f"{path}[{index}]") for index, child in enumerate(value)]
    return value


def compile_api_template(
    template: dict[str, Any],
    bindings: dict[str, Any],
    *,
    object_info: dict[str, Any] | None = None,
    reject_unused_bindings: bool = True,
) -> CompiledGraph:
    if not isinstance(template, dict) or not template:
        raise GraphValidationError("API graph template must be a non-empty object")
    if not isinstance(bindings, dict):
        raise GraphValidationError("bindings must be an object")
    used: set[str] = set()
    graph = _compile(template, bindings, used, "graph")
    if reject_unused_bindings:
        unused = sorted(set(bindings) - used)
        if unused:
            raise GraphValidationError(f"unused bindings: {unused}")
    validation = validate_api_graph(graph, object_info) if object_info is not None else None
    if validation is not None and not validation["valid"]:
        raise GraphValidationError(
            f"compiled graph failed live schema validation: {validation['issues']}"
        )
    return CompiledGraph(
        graph=graph,
        graph_hash=content_hash(graph),
        used_bindings=tuple(sorted(used)),
        validation=validation,
    )

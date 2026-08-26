"""Durable, neutral execution of an explicit serial graph plan."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

from .artifacts import artifacts_from_history, download_artifact
from .canonical import content_hash
from .client import ComfyClient
from .jobs import submit_graph, wait_for_job
from .probe import probe_runtime
from .receipts import build_run_receipt, save_receipt
from .schema import validate_api_graph


SERIES_SCHEMA = "comfy.run-series/1"
STATE_SCHEMA = "comfy.run-series-state/1"
STEP_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class SeriesStep:
    id: str
    graph_path: Path
    operation_ref_path: Path
    depends_on: str | None


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _safe_relative(root: Path, value: Any, label: str) -> Path:
    relative = PurePosixPath(str(value))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"series {label} must be a safe relative path")
    return root / Path(*relative.parts)


def validate_series_plan(plan: Any, *, root: Path) -> tuple[list[SeriesStep], str]:
    if not isinstance(plan, dict) or plan.get("schema") != SERIES_SCHEMA:
        raise ValueError(f"run series must use schema {SERIES_SCHEMA}")
    if set(plan) != {"schema", "id", "steps"}:
        raise ValueError("run series has unexpected or missing fields")
    if not isinstance(plan.get("id"), str) or not STEP_ID.fullmatch(plan["id"]):
        raise ValueError("run series id must be a stable lowercase identifier")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("run series requires one or more steps")

    steps: list[SeriesStep] = []
    ids: set[str] = set()
    previous: str | None = None
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "graph",
            "operation_ref",
            "depends_on",
        }:
            raise ValueError(f"malformed run-series step {raw!r}")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id) or step_id in ids:
            raise ValueError(f"invalid or duplicate run-series step id {step_id!r}")
        depends_on = raw.get("depends_on")
        if depends_on != previous:
            raise ValueError(
                f"step {step_id!r} depends_on must be the immediately preceding step {previous!r}"
            )
        graph = _safe_relative(root, raw.get("graph"), "graph")
        operation_ref = _safe_relative(root, raw.get("operation_ref"), "operation_ref")
        if not graph.is_file() or not operation_ref.is_file():
            raise ValueError(f"step {step_id!r} references a missing graph or operation_ref")
        steps.append(SeriesStep(step_id, graph, operation_ref, depends_on))
        ids.add(step_id)
        previous = step_id
    return steps, content_hash(plan)


def _fresh_state(plan_id: str, plan_hash: str, steps: list[SeriesStep]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "steps": [
            {
                "id": step.id,
                "status": "planned",
                "prompt_id": None,
                "receipt": None,
                "receipt_hash": None,
            }
            for step in steps
        ],
    }
    value["state_hash"] = content_hash(value)
    return value


def validate_series_state(
    state: Any,
    *,
    plan_id: str,
    plan_hash: str,
    steps: list[SeriesStep],
) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise ValueError(f"run-series state must use schema {STATE_SCHEMA}")
    supplied_hash = state.get("state_hash")
    unhashed = {key: value for key, value in state.items() if key != "state_hash"}
    if supplied_hash != content_hash(unhashed):
        raise ValueError("run-series state hash does not match its contents")
    if state.get("plan_id") != plan_id or state.get("plan_hash") != plan_hash:
        raise ValueError("run-series state belongs to another plan or revision")
    entries = state.get("steps")
    if not isinstance(entries, list) or len(entries) != len(steps):
        raise ValueError("run-series state step count differs from the plan")
    seen_incomplete = False
    for entry, step in zip(entries, steps, strict=True):
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "status",
            "prompt_id",
            "receipt",
            "receipt_hash",
        }:
            raise ValueError("run-series state contains a malformed step")
        if entry["id"] != step.id or entry["status"] not in {
            "planned",
            "submitted",
            "completed",
        }:
            raise ValueError("run-series state step identity or status is invalid")
        if entry["status"] == "completed":
            if (
                seen_incomplete
                or not entry["prompt_id"]
                or not entry["receipt"]
                or not entry["receipt_hash"]
            ):
                raise ValueError("completed run-series steps must form a valid prefix")
        else:
            seen_incomplete = True
        if entry["status"] == "submitted" and not entry["prompt_id"]:
            raise ValueError("submitted run-series step requires its exact prompt id")
    return state


def save_series_state(path: Path, state: dict[str, Any]) -> Path:
    unhashed = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = content_hash(unhashed)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def run_series(
    client: ComfyClient,
    plan: dict[str, Any],
    *,
    plan_root: Path,
    state_path: Path,
    receipts_dir: Path,
    downloads_dir: Path | None = None,
    timeout: float = 3600.0,
    interval: float = 5.0,
) -> dict[str, Any]:
    """Validate once, submit serially, and resume exact submitted prompt ids."""

    steps, plan_hash = validate_series_plan(plan, root=plan_root)
    manifest = probe_runtime(client)
    object_info = manifest.get("_captured_object_info")
    loaded: list[tuple[SeriesStep, dict[str, Any], dict[str, Any]]] = []
    for step in steps:
        graph = _load_object(step.graph_path)
        operation_ref = _load_object(step.operation_ref_path)
        validation = validate_api_graph(graph, object_info)
        if not validation["valid"]:
            raise ValueError(f"series step {step.id!r} failed live graph validation: {validation}")
        loaded.append((step, graph, operation_ref))

    if state_path.exists():
        state = validate_series_state(
            _load_object(state_path),
            plan_id=plan["id"],
            plan_hash=plan_hash,
            steps=steps,
        )
    else:
        state = _fresh_state(plan["id"], plan_hash, steps)
        save_series_state(state_path, state)

    downloaded: list[str] = []
    for index, (step, graph, operation_ref) in enumerate(loaded):
        entry = state["steps"][index]
        if entry["status"] == "completed":
            continue
        if entry["status"] == "planned":
            submitted = submit_graph(
                client,
                graph,
                extra_data={
                    "runtime_control": {
                        "series_id": plan["id"],
                        "series_step": step.id,
                        "operation": operation_ref,
                    }
                },
            )
            entry["status"] = "submitted"
            entry["prompt_id"] = submitted.prompt_id
            save_series_state(state_path, state)

        prompt_id = str(entry["prompt_id"])
        # Always use the same completion path for newly submitted and resumed jobs.
        # wait_for_job polls the exact prompt id and also rejects failed histories;
        # a plain history lookup would otherwise misclassify a failed job as complete.
        history = wait_for_job(client, prompt_id, timeout=timeout, interval=interval)
        artifacts = artifacts_from_history(history)
        receipt = build_run_receipt(
            operation_ref=operation_ref,
            api_graph=graph,
            runtime_manifest=manifest,
            prompt_id=prompt_id,
            history=history,
            artifacts=artifacts,
            evidence_status="executes",
        )
        receipt_path = receipts_dir / f"{step.id}-{prompt_id}.json"
        save_receipt(receipt_path, receipt)
        if downloads_dir is not None:
            for artifact in artifacts:
                downloaded.append(str(download_artifact(client, artifact, downloads_dir)))
        entry["status"] = "completed"
        entry["receipt"] = str(receipt_path)
        entry["receipt_hash"] = receipt["receipt_hash"]
        save_series_state(state_path, state)

    return {
        "schema": "comfy.run-series-result/1",
        "plan_id": plan["id"],
        "plan_hash": plan_hash,
        "state": str(state_path),
        "completed_steps": [entry["id"] for entry in state["steps"] if entry["status"] == "completed"],
        "prompt_ids": [entry["prompt_id"] for entry in state["steps"]],
        "receipts": [entry["receipt"] for entry in state["steps"]],
        "downloads": downloaded,
    }

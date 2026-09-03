"""Durable execution of independent, prebound ComfyUI API graphs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

from .artifacts import artifacts_from_history, download_artifact, unique_physical_artifacts
from .canonical import content_hash
from .client import ComfyClient
from .errors import JobExecutionError
from .jobs import job_history, submit_graph, wait_for_job
from .probe import probe_runtime
from .receipts import build_run_receipt, save_receipt
from .schema import validate_api_graph


BATCH_SCHEMA = "comfy.run-batch/1"
BATCH_STATE_SCHEMA = "comfy.run-batch-state/1"
STEP_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class BatchStep:
    id: str
    graph_path: Path
    operation_ref_path: Path


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _safe_relative(root: Path, value: Any, label: str) -> Path:
    relative = PurePosixPath(str(value))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"batch {label} must be a safe relative path")
    return root / Path(*relative.parts)


def validate_batch_plan(plan: Any, *, root: Path) -> tuple[list[BatchStep], str]:
    if not isinstance(plan, dict) or plan.get("schema") != BATCH_SCHEMA:
        raise ValueError(f"run batch must use schema {BATCH_SCHEMA}")
    if set(plan) != {"schema", "id", "steps"}:
        raise ValueError("run batch has unexpected or missing fields")
    if not isinstance(plan.get("id"), str) or not STEP_ID.fullmatch(plan["id"]):
        raise ValueError("run batch id must be a stable lowercase identifier")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("run batch requires one or more steps")

    steps: list[BatchStep] = []
    ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) != {"id", "graph", "operation_ref"}:
            raise ValueError(f"malformed run-batch step {raw!r}")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id) or step_id in ids:
            raise ValueError(f"invalid or duplicate run-batch step id {step_id!r}")
        graph = _safe_relative(root, raw.get("graph"), "graph")
        operation_ref = _safe_relative(root, raw.get("operation_ref"), "operation_ref")
        if not graph.is_file() or not operation_ref.is_file():
            raise ValueError(f"step {step_id!r} references a missing graph or operation_ref")
        steps.append(BatchStep(step_id, graph, operation_ref))
        ids.add(step_id)
    return steps, content_hash(plan)


def _fresh_state(plan_id: str, plan_hash: str, steps: list[BatchStep]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": BATCH_STATE_SCHEMA,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "steps": [
            {
                "id": step.id,
                "status": "planned",
                "prompt_id": None,
                "receipt": None,
                "receipt_hash": None,
                "error": None,
            }
            for step in steps
        ],
    }
    value["state_hash"] = content_hash(value)
    return value


def validate_batch_state(
    state: Any,
    *,
    plan_id: str,
    plan_hash: str,
    steps: list[BatchStep],
) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schema") != BATCH_STATE_SCHEMA:
        raise ValueError(f"run-batch state must use schema {BATCH_STATE_SCHEMA}")
    supplied_hash = state.get("state_hash")
    unhashed = {key: value for key, value in state.items() if key != "state_hash"}
    if supplied_hash != content_hash(unhashed):
        raise ValueError("run-batch state hash does not match its contents")
    if state.get("plan_id") != plan_id or state.get("plan_hash") != plan_hash:
        raise ValueError("run-batch state belongs to another plan or revision")
    entries = state.get("steps")
    if not isinstance(entries, list) or len(entries) != len(steps):
        raise ValueError("run-batch state step count differs from the plan")
    for entry, step in zip(entries, steps, strict=True):
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "status",
            "prompt_id",
            "receipt",
            "receipt_hash",
            "error",
        }:
            raise ValueError("run-batch state contains a malformed step")
        if entry["id"] != step.id or entry["status"] not in {
            "planned",
            "submitted",
            "completed",
            "failed",
        }:
            raise ValueError("run-batch state step identity or status is invalid")
        if entry["status"] == "planned" and any(
            entry[key] is not None
            for key in ("prompt_id", "receipt", "receipt_hash", "error")
        ):
            raise ValueError("planned run-batch step cannot contain execution evidence")
        if entry["status"] == "submitted" and (
            not entry["prompt_id"]
            or entry["receipt"] is not None
            or entry["receipt_hash"] is not None
            or entry["error"] is not None
        ):
            raise ValueError("submitted run-batch step requires only its exact prompt id")
        if entry["status"] == "completed" and not all(
            entry[key] for key in ("prompt_id", "receipt", "receipt_hash")
        ):
            raise ValueError("completed run-batch step requires prompt and receipt evidence")
        if entry["status"] == "completed" and entry["error"] is not None:
            raise ValueError("completed run-batch step cannot contain an error")
        if entry["status"] == "failed" and (
            not all(entry[key] for key in ("prompt_id", "receipt", "receipt_hash", "error"))
        ):
            raise ValueError("failed run-batch step requires prompt, receipt, and error evidence")
    return state


def save_batch_state(path: Path, state: dict[str, Any]) -> Path:
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


def run_batch(
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
    """Validate once, execute independent graphs sequentially, and resume exact ids."""

    steps, plan_hash = validate_batch_plan(plan, root=plan_root)
    manifest = probe_runtime(client)
    object_info = manifest.get("_captured_object_info")
    loaded: list[tuple[BatchStep, dict[str, Any], dict[str, Any]]] = []
    for step in steps:
        graph = _load_object(step.graph_path)
        operation_ref = _load_object(step.operation_ref_path)
        validation = validate_api_graph(graph, object_info)
        if not validation["valid"]:
            raise ValueError(f"batch step {step.id!r} failed live graph validation: {validation}")
        loaded.append((step, graph, operation_ref))

    if state_path.exists():
        state = validate_batch_state(
            _load_object(state_path),
            plan_id=plan["id"],
            plan_hash=plan_hash,
            steps=steps,
        )
    else:
        state = _fresh_state(plan["id"], plan_hash, steps)
        save_batch_state(state_path, state)

    downloaded: list[str] = []
    for index, (step, graph, operation_ref) in enumerate(loaded):
        entry = state["steps"][index]
        if entry["status"] in {"completed", "failed"}:
            continue
        if entry["status"] == "planned":
            submitted = submit_graph(
                client,
                graph,
                extra_data={
                    "runtime_control": {
                        "batch_id": plan["id"],
                        "batch_step": step.id,
                        "operation": operation_ref,
                    }
                },
            )
            entry["status"] = "submitted"
            entry["prompt_id"] = submitted.prompt_id
            save_batch_state(state_path, state)

        prompt_id = str(entry["prompt_id"])
        evidence_status = "executes"
        try:
            history = wait_for_job(client, prompt_id, timeout=timeout, interval=interval)
        except JobExecutionError as exc:
            history = job_history(client, prompt_id)
            entry["error"] = str(exc)
            evidence_status = "blocked"
        if history is None:
            raise RuntimeError(f"terminal job {prompt_id} has no history record")
        artifacts = artifacts_from_history(history)
        receipt = build_run_receipt(
            operation_ref=operation_ref,
            api_graph=graph,
            runtime_manifest=manifest,
            prompt_id=prompt_id,
            history=history,
            artifacts=artifacts,
            evidence_status=evidence_status,
        )
        receipt_path = receipts_dir / f"{step.id}-{prompt_id}.json"
        save_receipt(receipt_path, receipt)
        if downloads_dir is not None:
            downloadable = [
                artifact
                for artifact in unique_physical_artifacts(artifacts)
                if artifact.artifact_type == "output"
            ]
            for artifact in downloadable:
                downloaded.append(
                    str(download_artifact(client, artifact, downloads_dir, overwrite=True))
                )
        entry["status"] = "completed" if evidence_status == "executes" else "failed"
        entry["receipt"] = str(receipt_path)
        entry["receipt_hash"] = receipt["receipt_hash"]
        save_batch_state(state_path, state)

    return {
        "schema": "comfy.run-batch-result/1",
        "plan_id": plan["id"],
        "plan_hash": plan_hash,
        "state": str(state_path),
        "completed_steps": [
            entry["id"] for entry in state["steps"] if entry["status"] == "completed"
        ],
        "failed_steps": [
            entry["id"] for entry in state["steps"] if entry["status"] == "failed"
        ],
        "prompt_ids": [entry["prompt_id"] for entry in state["steps"]],
        "receipts": [entry["receipt"] for entry in state["steps"]],
        "downloads": downloaded,
    }

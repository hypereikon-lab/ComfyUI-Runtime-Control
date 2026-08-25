"""Immutable run-receipt construction and atomic persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .artifacts import ArtifactRef
from .canonical import content_hash


def build_run_receipt(
    *,
    operation_ref: dict[str, Any],
    api_graph: dict[str, Any],
    runtime_manifest: dict[str, Any],
    prompt_id: str,
    history: dict[str, Any] | None,
    artifacts: list[ArtifactRef],
    evidence_status: str,
) -> dict[str, Any]:
    allowed_status = {"schema-validated", "executes", "visually-accepted", "rejected", "blocked"}
    if evidence_status not in allowed_status:
        raise ValueError(f"evidence_status must be one of {sorted(allowed_status)}")
    required_operation = {"id", "version", "contract_hash"}
    if not isinstance(operation_ref, dict) or not required_operation <= set(operation_ref):
        raise ValueError("operation_ref requires id, version, and contract_hash")
    operation_id = operation_ref["id"]
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", operation_id
    ):
        raise ValueError("operation_ref id must be a semantic dotted identifier")
    operation_version = operation_ref["version"]
    if not isinstance(operation_version, int) or operation_version < 1:
        raise ValueError("operation_ref version must be a positive integer")
    operation_hash = operation_ref["contract_hash"]
    if not isinstance(operation_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", operation_hash):
        raise ValueError("operation_ref contract_hash must be lowercase SHA-256")
    receipt: dict[str, Any] = {
        "schema": "comfy.run-receipt/2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt_id": prompt_id,
        "operation": operation_id,
        "operation_version": operation_version,
        "operation_contract_hash": operation_hash,
        "evidence_status": evidence_status,
        "api_graph_hash": content_hash(api_graph),
        "runtime_manifest_hash": runtime_manifest.get("manifest_hash"),
        "history_hash": content_hash(history) if history is not None else None,
        "artifacts": [value.as_dict() for value in artifacts],
    }
    receipt["receipt_hash"] = content_hash(receipt)
    return receipt


def save_receipt(path: str | Path, receipt: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target

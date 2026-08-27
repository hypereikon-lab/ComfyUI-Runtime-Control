"""Narrow Manager adapter with exact-target mutation guards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from .client import ComfyClient
from .errors import ComfyRuntimeError, MutationGuardError


@dataclass(frozen=True)
class MutationPlan:
    operation: str
    target: str
    route: str
    payload: dict[str, Any]


GIT_INSTALL_JOURNAL_SCHEMA = "comfy.git-install-journal/1"
GIT_INSTALL_PLAN_SCHEMA = "comfy.git-install-plan/1"
RECOVERY_CHANNELS = frozenset({"external-operator", "process-supervisor"})


@dataclass(frozen=True)
class GitInstallPlan:
    source_url: str
    repository: str
    visibility: str
    default_branch: str
    recovery_channel: str
    route: str = "/customnode/install/git_url"

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": GIT_INSTALL_PLAN_SCHEMA,
            "source_url": self.source_url,
            "repository": self.repository,
            "visibility": self.visibility,
            "default_branch": self.default_branch,
            "recovery_channel": self.recovery_channel,
            "route": self.route,
        }


def plan_custom_node_update(
    target: str,
    *,
    version: str = "unknown",
    source_url: str | None = None,
) -> MutationPlan:
    normalized = target.strip()
    if not normalized or normalized in {"*", "all", "update-all"}:
        raise ValueError("an exact custom-node identifier is required")
    normalized_version = version.strip()
    if not normalized_version:
        raise ValueError("version cannot be empty")
    payload: dict[str, Any] = {
        "id": normalized,
        "ui_id": normalized,
        "version": normalized_version,
    }
    if normalized_version == "unknown":
        if source_url is None:
            raise ValueError("source_url is required for an unknown Git package")
        _validate_git_url(source_url)
        payload["files"] = [source_url]
    return MutationPlan(
        operation="custom-node-update",
        target=normalized,
        route="/manager/queue/update",
        payload=payload,
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


def plan_git_install(
    source_url: str,
    *,
    visibility_confirmation: str,
    default_branch: str,
    recovery_channel: str,
) -> GitInstallPlan:
    """Plan one public GitHub install without contacting the runtime."""

    normalized = source_url.rstrip("/")
    _validate_git_url(normalized)
    if visibility_confirmation != "public":
        raise MutationGuardError("visibility_confirmation must be public")
    branch = default_branch.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ValueError("default_branch must be one exact branch name")
    if recovery_channel not in RECOVERY_CHANNELS:
        allowed = ", ".join(sorted(RECOVERY_CHANNELS))
        raise MutationGuardError(f"recovery_channel must be one of: {allowed}")
    repository = normalized.removesuffix(".git").rsplit("/", 1)[-1]
    return GitInstallPlan(
        source_url=normalized,
        repository=repository,
        visibility="public",
        default_branch=branch,
        recovery_channel=recovery_channel,
    )


def install_git_url(
    client: ComfyClient,
    plan: GitInstallPlan,
    *,
    confirmation: str,
    journal_path: str | Path,
) -> dict[str, Any]:
    """Submit one exact install after atomically persisting its mutation intent.

    A transport failure after submission is deliberately recorded as
    ``outcome-unknown``. Call :func:`reconcile_git_install` before considering
    any retry; Manager may still be cloning after the HTTP connection vanished.
    """

    if confirmation.rstrip("/") != plan.source_url:
        raise MutationGuardError("confirmation must exactly match the Git URL")
    journal = Path(journal_path)
    if journal.exists():
        raise MutationGuardError("journal_path already exists; reconcile it instead of retrying")

    record: dict[str, Any] = {
        "schema": GIT_INSTALL_JOURNAL_SCHEMA,
        "recorded_at": _now(),
        "updated_at": _now(),
        "state": "submission-intent-recorded",
        "attempt_count": 1,
        "plan": plan.as_dict(),
        "result": None,
        "error": None,
        "reconciliation": None,
    }
    _write_journal(journal, record, create_only=True)

    try:
        result = _request_git_install(client, plan.source_url)
    except Exception as exc:
        record["state"] = "outcome-unknown"
        record["updated_at"] = _now()
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_journal(journal, record)
        raise MutationGuardError(
            f"install outcome is unknown; do not retry; reconcile journal {journal}"
        ) from exc

    record["state"] = "acknowledged"
    record["updated_at"] = _now()
    record["result"] = result
    _write_journal(journal, record)
    return record


def reconcile_git_install(client: ComfyClient, journal_path: str | Path) -> dict[str, Any]:
    """Reconcile an install journal against Manager's installed-node inventory."""

    journal = Path(journal_path)
    record = _read_journal(journal)
    plan = record.get("plan")
    if record.get("schema") != GIT_INSTALL_JOURNAL_SCHEMA or not isinstance(plan, dict):
        raise ValueError("not a supported Git-install journal")
    source_url = plan.get("source_url")
    repository = plan.get("repository")
    if not isinstance(source_url, str) or not isinstance(repository, str):
        raise ValueError("journal is missing its exact repository identity")

    inventory = client.get("/customnode/installed")
    matches = _find_inventory_matches(inventory, source_url, repository)
    reconciled_at = _now()
    if matches:
        state = "reconciled-installed"
        manual_check_required = False
    else:
        state = "reconciled-not-listed"
        manual_check_required = True
    record["state"] = state
    record["updated_at"] = reconciled_at
    record["reconciliation"] = {
        "checked_at": reconciled_at,
        "route": "/customnode/installed",
        "matches": matches,
        "manual_partial_directory_check_required": manual_check_required,
        "retry_authorized": False,
    }
    _write_journal(journal, record)
    return record


def _request_git_install(client: ComfyClient, normalized: str) -> str:
    try:
        _, body = client.request_bytes(
            "POST",
            "/customnode/install/git_url",
            json_body={"url": normalized},
        )
    except ComfyRuntimeError as exc:
        # The fallback is allowed only for a definite 400 rejection. A timeout
        # or any other status can mean Manager is still mutating the filesystem.
        if "unexpected HTTP 400" not in str(exc):
            raise
        _, body = client.request_bytes(
            "POST",
            "/customnode/install/git_url",
            data=normalized.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    if not body:
        return "installed"
    decoded = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return decoded
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("status", "message", "result"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _find_inventory_matches(inventory: Any, source_url: str, repository: str) -> list[str]:
    source = source_url.removesuffix(".git").casefold()
    repo = repository.casefold()
    matches: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")
        elif isinstance(value, str):
            normalized = value.removesuffix(".git").rstrip("/").casefold()
            if normalized == source or normalized == repo:
                matches.append(path)

    visit(inventory, "$")
    return sorted(set(matches))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_journal(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("mutation journal must contain one JSON object")
    return value


def _write_journal(path: Path, value: dict[str, Any], *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only and path.exists():
        raise MutationGuardError("journal_path already exists")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_git_url(value: str) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        raise ValueError("only an exact public HTTPS GitHub repository URL is allowed")

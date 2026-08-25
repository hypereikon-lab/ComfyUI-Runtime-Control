"""Command-line entry point for repeatable remote ComfyUI operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .artifacts import artifacts_from_history, download_artifact
from .client import ComfyClient, RuntimeConfig
from .jobs import job_history, submit_graph, wait_for_job
from .manager import apply_mutation, plan_custom_node_update, reboot_comfy
from .probe import probe_runtime, public_manifest
from .receipts import build_run_receipt, save_receipt
from .schema import dependency_plan, validate_api_graph


def _json_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _client(args: argparse.Namespace) -> ComfyClient:
    return ComfyClient(
        RuntimeConfig(
            base_url=args.url,
            timeout=args.request_timeout,
            client_id=args.client_id or "",
            access_client_id=os.environ.get("CF_ACCESS_CLIENT_ID"),
            access_client_secret=os.environ.get("CF_ACCESS_CLIENT_SECRET"),
        )
    )


def _probe(args: argparse.Namespace) -> int:
    _print(public_manifest(probe_runtime(_client(args))))
    return 0


def _validate(args: argparse.Namespace) -> int:
    client = _client(args)
    graph = _json_file(args.graph)
    object_info = client.get("/object_info")
    report = validate_api_graph(graph, object_info)
    _print(report)
    return 0 if report["valid"] else 2


def _dependencies(args: argparse.Namespace) -> int:
    client = _client(args)
    report = dependency_plan(_json_file(args.graph), client.get("/object_info"))
    _print(report)
    return 0 if report["ready"] else 2


def _run(args: argparse.Namespace) -> int:
    client = _client(args)
    graph = _json_file(args.graph)
    workflow_spec = _json_file(args.spec) if args.spec else None
    manifest = probe_runtime(client)
    validation = validate_api_graph(graph, manifest.get("_captured_object_info"))
    if not validation["valid"]:
        _print(validation)
        return 2
    submitted = submit_graph(client, graph, extra_data={"runtime_control": {"validation": validation}})
    history = wait_for_job(
        client,
        submitted.prompt_id,
        timeout=args.job_timeout,
        interval=args.poll_interval,
    )
    artifacts = artifacts_from_history(history)
    receipt = build_run_receipt(
        workflow_spec=workflow_spec,
        api_graph=graph,
        runtime_manifest=manifest,
        prompt_id=submitted.prompt_id,
        history=history,
        artifacts=artifacts,
        evidence_status="executes",
    )
    receipt_path = Path(args.receipts) / f"{submitted.prompt_id}.json"
    save_receipt(receipt_path, receipt)
    downloaded: list[str] = []
    if args.downloads:
        for artifact in artifacts:
            downloaded.append(str(download_artifact(client, artifact, args.downloads)))
    _print(
        {
            "prompt_id": submitted.prompt_id,
            "receipt": str(receipt_path),
            "artifacts": [value.as_dict() for value in artifacts],
            "downloads": downloaded,
            "evidence_status": "executes",
        }
    )
    return 0


def _history(args: argparse.Namespace) -> int:
    _print(job_history(_client(args), args.prompt_id))
    return 0


def _upload(args: argparse.Namespace) -> int:
    response = _client(args).upload_image(
        args.source,
        subfolder=args.subfolder,
        overwrite=args.overwrite,
        upload_type=args.type,
    )
    _print(response)
    return 0


def _plan_update(args: argparse.Namespace) -> int:
    plan = plan_custom_node_update(args.target)
    _print({"operation": plan.operation, "target": plan.target, "route": plan.route, "payload": plan.payload})
    return 0


def _apply_update(args: argparse.Namespace) -> int:
    plan = plan_custom_node_update(args.target)
    _print(apply_mutation(_client(args), plan, confirmation=args.confirm))
    return 0


def _restart(args: argparse.Namespace) -> int:
    _print(reboot_comfy(_client(args), confirmation=args.confirm))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comfy-runtime")
    parser.add_argument("--url", required=True, help="ComfyUI origin, for example https://host")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--client-id")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="R1: capture a runtime manifest")
    probe.set_defaults(handler=_probe)

    validate = subparsers.add_parser("validate", help="R2: validate an API graph")
    validate.add_argument("graph")
    validate.set_defaults(handler=_validate)

    dependencies = subparsers.add_parser("dependencies", help="R5: plan node dependencies")
    dependencies.add_argument("graph")
    dependencies.set_defaults(handler=_dependencies)

    run = subparsers.add_parser("run", help="R2/R3/R4/R8: validate, submit, wait, and receipt")
    run.add_argument("graph")
    run.add_argument("--spec")
    run.add_argument("--job-timeout", type=float, default=3600.0)
    run.add_argument("--poll-interval", type=float, default=5.0)
    run.add_argument("--receipts", default="receipts")
    run.add_argument("--downloads")
    run.set_defaults(handler=_run)

    history = subparsers.add_parser("history", help="inspect one exact job")
    history.add_argument("prompt_id")
    history.set_defaults(handler=_history)

    upload = subparsers.add_parser("upload", help="upload one explicit input file")
    upload.add_argument("source")
    upload.add_argument("--subfolder", default="")
    upload.add_argument("--type", default="input", choices=("input", "temp"))
    upload.add_argument("--overwrite", action="store_true")
    upload.set_defaults(handler=_upload)

    plan_update = subparsers.add_parser("plan-update", help="R6: show a targeted Manager plan")
    plan_update.add_argument("target")
    plan_update.set_defaults(handler=_plan_update)

    apply_update = subparsers.add_parser("apply-update", help="R6: queue one exact Manager update")
    apply_update.add_argument("target")
    apply_update.add_argument("--confirm", required=True)
    apply_update.set_defaults(handler=_apply_update)

    restart = subparsers.add_parser("restart-comfy", help="R6: restart only the ComfyUI process")
    restart.add_argument("--confirm", required=True)
    restart.set_defaults(handler=_restart)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

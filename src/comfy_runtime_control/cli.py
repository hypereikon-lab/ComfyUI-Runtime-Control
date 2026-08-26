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
from .compiler import compile_api_template
from .jobs import job_history, submit_graph, wait_for_job
from .manager import apply_mutation, install_git_url, plan_custom_node_update, reboot_comfy
from .materialization import materialize_workspace_export, write_materialized_draft
from .probe import probe_runtime, public_manifest
from .receipts import build_run_receipt, save_receipt
from .schema import dependency_plan, validate_api_graph
from .series import run_series


def _json_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _client(args: argparse.Namespace) -> ComfyClient:
    if not args.url:
        raise ValueError("--url is required for commands that access ComfyUI")
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
    manifest = probe_runtime(_client(args))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    result = public_manifest(manifest)
    result["full_manifest_output"] = str(Path(args.output)) if args.output else None
    _print(result)
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


def _compile_graph(args: argparse.Namespace) -> int:
    client = _client(args)
    compiled = compile_api_template(
        _json_file(args.template),
        _json_file(args.bindings),
        object_info=client.get("/object_info"),
        reject_unused_bindings=not args.allow_unused_bindings,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(compiled.graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _print(
        {
            "output": str(output),
            "api_graph_hash": compiled.graph_hash,
            "used_bindings": list(compiled.used_bindings),
            "validation": compiled.validation,
        }
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    client = _client(args)
    graph = _json_file(args.graph)
    operation_ref = _json_file(args.operation_ref)
    manifest = probe_runtime(client)
    validation = validate_api_graph(graph, manifest.get("_captured_object_info"))
    if not validation["valid"]:
        _print(validation)
        return 2
    submitted = submit_graph(
        client,
        graph,
        extra_data={"runtime_control": {"validation": validation, "operation": operation_ref}},
    )
    history = wait_for_job(
        client,
        submitted.prompt_id,
        timeout=args.job_timeout,
        interval=args.poll_interval,
    )
    artifacts = artifacts_from_history(history)
    receipt = build_run_receipt(
        operation_ref=operation_ref,
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


def _run_series(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    result = run_series(
        _client(args),
        _json_file(str(plan_path)),
        plan_root=plan_path.parent,
        state_path=Path(args.state),
        receipts_dir=Path(args.receipts),
        downloads_dir=Path(args.downloads) if args.downloads else None,
        timeout=args.job_timeout,
        interval=args.poll_interval,
    )
    _print(result)
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
    plan = plan_custom_node_update(
        args.target, version=args.version, source_url=args.source_url
    )
    _print({"operation": plan.operation, "target": plan.target, "route": plan.route, "payload": plan.payload})
    return 0


def _apply_update(args: argparse.Namespace) -> int:
    plan = plan_custom_node_update(
        args.target, version=args.version, source_url=args.source_url
    )
    _print(apply_mutation(_client(args), plan, confirmation=args.confirm))
    return 0


def _restart(args: argparse.Namespace) -> int:
    _print(reboot_comfy(_client(args), confirmation=args.confirm))
    return 0


def _install_git(args: argparse.Namespace) -> int:
    _print(
        {
            "source_url": args.source_url,
            "result": install_git_url(
                _client(args), args.source_url, confirmation=args.confirm
            ),
        }
    )
    return 0


def _materialize_export(args: argparse.Namespace) -> int:
    runtime_manifest = _json_file(args.runtime_manifest) if args.runtime_manifest else None
    draft = materialize_workspace_export(
        _json_file(args.workspace_export),
        _json_file(args.parameterization),
        _json_file(args.operation_ref),
        args.variant,
        runtime_manifest=runtime_manifest,
    )
    paths = write_materialized_draft(args.output_dir, draft, overwrite=args.overwrite)
    _print({"manifest": draft.manifest, "files": paths})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comfy-runtime")
    parser.add_argument("--url", help="ComfyUI origin, for example https://host")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--client-id")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="R1: capture a runtime manifest")
    probe.add_argument("--output", help="persist the full manifest including object_info")
    probe.set_defaults(handler=_probe)

    validate = subparsers.add_parser("validate", help="R2: validate an API graph")
    validate.add_argument("graph")
    validate.set_defaults(handler=_validate)

    dependencies = subparsers.add_parser("dependencies", help="R5: plan node dependencies")
    dependencies.add_argument("graph")
    dependencies.set_defaults(handler=_dependencies)

    compile_graph = subparsers.add_parser("compile", help="R2: bind and live-validate an API graph template")
    compile_graph.add_argument("template")
    compile_graph.add_argument("bindings")
    compile_graph.add_argument("--output", required=True)
    compile_graph.add_argument("--allow-unused-bindings", action="store_true")
    compile_graph.set_defaults(handler=_compile_graph)

    run = subparsers.add_parser("run", help="R2/R3/R4/R8: validate, submit, wait, and receipt")
    run.add_argument("graph")
    run.add_argument("--operation-ref", required=True)
    run.add_argument("--job-timeout", type=float, default=3600.0)
    run.add_argument("--poll-interval", type=float, default=5.0)
    run.add_argument("--receipts", default="receipts")
    run.add_argument("--downloads")
    run.set_defaults(handler=_run)

    run_series_parser = subparsers.add_parser(
        "run-series",
        help="R10: validate and execute a durable serial graph plan",
    )
    run_series_parser.add_argument("plan")
    run_series_parser.add_argument("--state", required=True)
    run_series_parser.add_argument("--job-timeout", type=float, default=3600.0)
    run_series_parser.add_argument("--poll-interval", type=float, default=5.0)
    run_series_parser.add_argument("--receipts", default="receipts")
    run_series_parser.add_argument("--downloads")
    run_series_parser.set_defaults(handler=_run_series)

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
    plan_update.add_argument("--version", default="unknown")
    plan_update.add_argument("--source-url")
    plan_update.set_defaults(handler=_plan_update)

    apply_update = subparsers.add_parser("apply-update", help="R6: queue one exact Manager update")
    apply_update.add_argument("target")
    apply_update.add_argument("--version", default="unknown")
    apply_update.add_argument("--source-url")
    apply_update.add_argument("--confirm", required=True)
    apply_update.set_defaults(handler=_apply_update)

    restart = subparsers.add_parser("restart-comfy", help="R6: restart only the ComfyUI process")
    restart.add_argument("--confirm", required=True)
    restart.set_defaults(handler=_restart)

    install_git = subparsers.add_parser("install-git", help="R6: install one exact public GitHub custom-node repository")
    install_git.add_argument("source_url")
    install_git.add_argument("--confirm", required=True)
    install_git.set_defaults(handler=_install_git)

    materialize = subparsers.add_parser(
        "materialize-export",
        help="R2/R7: create a guarded variant-scoped UI/API draft from Workspace Control",
    )
    materialize.add_argument("workspace_export")
    materialize.add_argument("parameterization")
    materialize.add_argument("--operation-ref", required=True)
    materialize.add_argument("--variant", required=True)
    materialize.add_argument("--output-dir", required=True)
    materialize.add_argument("--runtime-manifest")
    materialize.add_argument("--overwrite", action="store_true")
    materialize.set_defaults(handler=_materialize_export)
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

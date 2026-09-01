from pathlib import Path
import json
import tempfile
import unittest

from comfy_runtime_control.artifacts import artifacts_from_history
from comfy_runtime_control.client import ComfyClient, RuntimeConfig
from comfy_runtime_control.canonical import content_hash
from comfy_runtime_control.compiler import compile_api_template
from comfy_runtime_control.errors import GraphValidationError, MutationGuardError
from comfy_runtime_control.jobs import submit_graph, wait_for_job
from comfy_runtime_control.manager import (
    apply_mutation,
    install_git_url,
    plan_custom_node_update,
    plan_git_install,
    reconcile_git_install,
)
from comfy_runtime_control.materialization import (
    materialize_workspace_export,
    parameterize_api_graph,
    write_materialized_draft,
)
from comfy_runtime_control.probe import (
    build_runtime_manifest,
    probe_runtime,
    public_manifest,
    validate_runtime_manifest,
)
from comfy_runtime_control.receipts import build_run_receipt, save_receipt
from comfy_runtime_control.requirements import (
    evaluate_runtime_requirements,
    validate_runtime_requirements,
)
from comfy_runtime_control.schema import dependency_plan, validate_api_graph
from comfy_runtime_control.series import (
    SERIES_SCHEMA,
    STATE_SCHEMA,
    run_series,
    validate_series_plan,
    validate_series_state,
)


OBJECT_INFO = {
    "LoadImage": {
        "input": {"required": {"image": [["a.png", "b.png"], {"image_upload": True}]}}
    },
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}},
}

GRAPH = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
    "2": {
        "class_type": "SaveImage",
        "inputs": {"images": ["1", 0], "filename_prefix": "test"},
    },
}

UI_GRAPH = {"nodes": [{"id": 1, "type": "LoadImage"}], "links": []}
WORKSPACE_EXPORT = {
    "schema": "comfy.workspace-export/1",
    "capturedAt": "2026-08-25T12:00:00Z",
    "activePath": "workflows/test.json",
    "uiGraph": UI_GRAPH,
    "apiGraph": GRAPH,
    "uiGraphSignature": content_hash(UI_GRAPH),
    "apiGraphSignature": content_hash(GRAPH),
}
PARAMETERIZATION = {
    "schema": "comfy.api-parameterization/1",
    "parameters": [
        {"name": "input_filename", "pointers": ["/1/inputs/image"], "expected": "a.png"},
        {
            "name": "output_prefix",
            "pointers": ["/2/inputs/filename_prefix"],
            "expected": "test",
        },
    ],
}
OPERATION_REF = {
    "id": "generate.keyframed",
    "version": 1,
    "contract_hash": "a" * 64,
}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.history_calls = 0

    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=30.0):
        self.calls.append((method, url, headers or {}, json_body, data))
        path = url.split("https://unit.invalid", 1)[-1].split("?", 1)[0]
        if path == "/object_info":
            value = OBJECT_INFO
        elif path == "/features":
            value = {"supports_preview_metadata": True}
        elif path == "/system_stats":
            value = {
                "system": {"comfyui_version": "test", "ram_total": 64_000_000_000},
                "devices": [
                    {"name": "NVIDIA RTX 5090", "type": "cuda", "vram_total": 32_000_000_000}
                ],
            }
        elif path == "/extensions":
            value = []
        elif path == "/models":
            value = []
        elif path == "/queue":
            value = {"queue_running": [], "queue_pending": []}
        elif path == "/prompt":
            value = {"prompt_id": "p1", "number": 1, "node_errors": {}}
        elif path == "/history/p1":
            self.history_calls += 1
            value = (
                {}
                if self.history_calls == 1
                else {
                    "p1": {
                        "status": {"completed": True},
                        "outputs": {
                            "2": {
                                "images": [
                                    {"filename": "x.png", "subfolder": "test", "type": "output"}
                                ]
                            }
                        },
                    }
                }
            )
        elif path in {"/manager/queue/reset", "/manager/queue/update", "/manager/queue/start"}:
            value = {"ok": True}
        elif path == "/customnode/install/git_url":
            value = {"status": "installed"}
        elif path == "/customnode/installed":
            value = {
                "custom_nodes": [
                    {
                        "id": "repository",
                        "files": ["https://github.com/owner/repository"],
                    }
                ]
            }
        else:
            return 404, {}, b"{}"
        return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()


class LegacyGitInstallTransport(FakeTransport):
    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=30.0):
        path = url.split("https://unit.invalid", 1)[-1].split("?", 1)[0]
        if path == "/customnode/install/git_url":
            self.calls.append((method, url, headers or {}, json_body, data))
            if json_body is not None:
                return 400, {"Content-Type": "text/plain"}, b"expected text/plain"
            return 200, {"Content-Type": "text/plain"}, b"installed"
        return super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            data=data,
            timeout=timeout,
        )


class UnknownGitInstallTransport(FakeTransport):
    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=30.0):
        path = url.split("https://unit.invalid", 1)[-1].split("?", 1)[0]
        if path == "/customnode/install/git_url":
            self.calls.append((method, url, headers or {}, json_body, data))
            raise TimeoutError("connection disappeared after submission")
        return super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            data=data,
            timeout=timeout,
        )


class SeriesTransport(FakeTransport):
    def __init__(self, *, resumed_prompt_id=None):
        super().__init__()
        self.prompt_count = 0
        self.resumed_prompt_id = resumed_prompt_id

    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=30.0):
        path = url.split("https://unit.invalid", 1)[-1].split("?", 1)[0]
        if path == "/prompt":
            self.calls.append((method, url, headers or {}, json_body, data))
            self.prompt_count += 1
            prompt_id = f"p{self.prompt_count}"
            value = {"prompt_id": prompt_id, "number": self.prompt_count, "node_errors": {}}
            return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()
        if path.startswith("/history/"):
            self.calls.append((method, url, headers or {}, json_body, data))
            prompt_id = path.rsplit("/", 1)[-1]
            allowed = {f"p{number}" for number in range(1, self.prompt_count + 1)}
            if self.resumed_prompt_id:
                allowed.add(self.resumed_prompt_id)
            value = (
                {
                    prompt_id: {
                        "status": {"completed": True},
                        "outputs": {
                            "2": {
                                "images": [
                                    {
                                        "filename": f"{prompt_id}.png",
                                        "subfolder": "series",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
                if prompt_id in allowed
                else {}
            )
            return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()
        return super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            data=data,
            timeout=timeout,
        )


class RuntimeTests(unittest.TestCase):
    def client(self):
        transport = FakeTransport()
        client = ComfyClient(
            RuntimeConfig(
                "https://unit.invalid",
                client_id="test-client",
                access_client_id="id",
                access_client_secret="secret",
            ),
            transport,
        )
        return client, transport

    def test_probe_hashes_runtime_without_leaking_access_secret(self):
        client, transport = self.client()
        manifest = probe_runtime(client)
        compact = public_manifest(manifest)
        self.assertEqual(compact["node_type_count"], 2)
        self.assertEqual(len(compact["manifest_hash"]), 64)
        self.assertNotIn("_captured_object_info", compact)
        self.assertNotIn("secret", json.dumps(compact))
        self.assertEqual(transport.calls[0][2]["CF-Access-Client-Id"], "id")
        self.assertEqual(
            transport.calls[0][2]["User-Agent"], "comfy-runtime-control/0.5.3"
        )
        self.assertEqual(validate_runtime_manifest(manifest), OBJECT_INFO)
        tampered = json.loads(json.dumps(manifest))
        tampered["_captured_object_info"]["LoadImage"]["input"]["required"]["image"][0] = []
        with self.assertRaisesRegex(ValueError, "object_info hash"):
            validate_runtime_manifest(tampered)

    def test_browser_snapshots_build_the_same_validated_manifest_contract(self):
        captured = {
            "features": {"supports_preview_metadata": True},
            "system_stats": {
                "system": {"ram_total": 64_000_000_000},
                "devices": [{"name": "RTX 5090", "vram_total": 32_000_000_000}],
            },
            "object_info": OBJECT_INFO,
            "models": [],
            "queue": {"queue_running": [], "queue_pending": []},
        }
        manifest = build_runtime_manifest(
            captured,
            runtime_label="authenticated-browser-handoff",
            captured_at="2026-08-26T12:00:00+00:00",
        )
        self.assertEqual(validate_runtime_manifest(manifest), OBJECT_INFO)
        self.assertFalse(manifest["endpoints"]["extensions"]["available"])
        self.assertEqual(manifest["endpoints"]["extensions"]["error"], "not captured")
        self.assertNotIn("_captured_queue", public_manifest(manifest))
        with self.assertRaisesRegex(ValueError, "unknown probe endpoint"):
            build_runtime_manifest({"cookie": "secret"}, runtime_label="bad")

    def test_graph_validation_and_dependency_plan(self):
        report = validate_api_graph(GRAPH, OBJECT_INFO)
        self.assertTrue(report["valid"])
        self.assertTrue(dependency_plan(GRAPH, OBJECT_INFO)["ready"])
        broken = dict(GRAPH)
        broken["3"] = {"class_type": "MissingNode", "inputs": {}}
        report = validate_api_graph(broken, OBJECT_INFO)
        self.assertFalse(report["valid"])
        self.assertIn("MissingNode", dependency_plan(broken, OBJECT_INFO)["missing_node_types"])

    def test_graph_validation_accepts_frontend_dynamic_combo_children_only(self):
        schema = {
            "SaveVideo": {
                "input": {
                    "required": {
                        "video": ["VIDEO"],
                        "format": ["COMFY_DYNAMICCOMBO_V3"],
                    }
                }
            }
        }
        graph = {
            "1": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": "opaque-video-fixture",
                    "format": "auto",
                    "format.codec": "auto",
                    "unknown.child": "rejected",
                },
            }
        }
        report = validate_api_graph(graph, schema)
        self.assertTrue(report["valid"])
        self.assertEqual(report["warning_count"], 1)
        self.assertEqual(report["issues"][0]["input_name"], "unknown.child")

    def test_runtime_requirements_gate_uses_captured_nodes_models_hardware_and_queue(self):
        client, _ = self.client()
        manifest = probe_runtime(client)
        requirements = {
            "schema": "comfy.runtime-requirements/1",
            "id": "unit-core",
            "required_endpoints": ["object_info", "system_stats", "queue"],
            "required_node_types": ["LoadImage", "SaveImage"],
            "node_type_groups": [
                {"id": "image-loader", "any_of": ["MissingLoader", "LoadImage"]}
            ],
            "required_models": ["a.png"],
            "hardware": {
                "minimum_total_ram_bytes": 60_000_000_000,
                "minimum_total_vram_bytes": 30_000_000_000,
                "device_name_contains": "5090",
            },
            "require_queue_idle": True,
            "manual_checks": ["confirm free storage outside ComfyUI"],
        }
        report = evaluate_runtime_requirements(requirements, manifest)
        self.assertTrue(report["ready"])
        self.assertEqual(report["checks"]["node_types"]["missing"], [])
        self.assertEqual(report["checks"]["models"]["missing"], [])
        self.assertTrue(report["checks"]["queue"]["observed_idle"])
        self.assertEqual(report["manual_checks"], ["confirm free storage outside ComfyUI"])

        broken = json.loads(json.dumps(requirements))
        broken["required_node_types"].append("MissingNode")
        broken["required_models"].append("missing.safetensors")
        failed = evaluate_runtime_requirements(broken, manifest)
        self.assertFalse(failed["ready"])
        self.assertEqual(failed["checks"]["node_types"]["missing"], ["MissingNode"])
        self.assertEqual(failed["checks"]["models"]["missing"], ["missing.safetensors"])

        malformed = json.loads(json.dumps(requirements))
        malformed["required_node_types"].append("LoadImage")
        with self.assertRaisesRegex(ValueError, "duplicates"):
            validate_runtime_requirements(malformed)

    def test_compiler_binds_and_live_validates_template(self):
        template = {
            "1": {
                "class_type": "LoadImage",
                "inputs": {"image": {"$binding": "input_filename"}},
            },
            "2": {
                "class_type": "SaveImage",
                "inputs": {
                    "images": ["1", 0],
                    "filename_prefix": {"$binding": "output_prefix"},
                },
            },
        }
        compiled = compile_api_template(
            template,
            {"input_filename": "a.png", "output_prefix": "compiled"},
            object_info=OBJECT_INFO,
        )
        self.assertEqual(compiled.graph["2"]["inputs"]["filename_prefix"], "compiled")
        self.assertTrue(compiled.validation["valid"])
        self.assertEqual(compiled.used_bindings, ("input_filename", "output_prefix"))

    def test_compiler_rejects_missing_unused_and_mixed_binding_objects(self):
        with self.assertRaisesRegex(GraphValidationError, "missing binding"):
            compile_api_template({"1": {"x": {"$binding": "x"}}}, {})
        with self.assertRaisesRegex(GraphValidationError, "unused bindings"):
            compile_api_template({"1": {"x": 1}}, {"unused": 2})
        with self.assertRaisesRegex(GraphValidationError, "cannot contain other keys"):
            compile_api_template(
                {"1": {"x": {"$binding": "x", "default": 1}}}, {"x": 2}
            )

    def test_workspace_export_materializes_a_round_trip_pair(self):
        client, _ = self.client()
        draft = materialize_workspace_export(
            WORKSPACE_EXPORT,
            PARAMETERIZATION,
            OPERATION_REF,
            "first-last",
            runtime_manifest=probe_runtime(client),
        )
        self.assertEqual(draft.manifest["state"], "schema-validated-draft")
        self.assertTrue(draft.manifest["round_trip"]["valid"])
        self.assertEqual(draft.manifest["promotion_gate"], "requires-live-review")
        self.assertEqual(
            draft.manifest["runtime_manifest"]["object_info_hash"],
            content_hash(OBJECT_INFO),
        )
        self.assertEqual(
            draft.api_template["1"]["inputs"]["image"],
            {"$binding": "input_filename"},
        )
        self.assertEqual(draft.bindings["output_prefix"], "test")
        self.assertEqual(
            draft.manifest["source"]["api_graph_hash"],
            content_hash(GRAPH),
        )

    def test_workspace_export_v2_preserves_extension_provenance(self):
        exported = dict(WORKSPACE_EXPORT)
        exported["schema"] = "comfy.workspace-export/2"
        exported["workspaceControlVersion"] = "0.4.0"
        exported["activeWorkflow"] = {
            "path": exported["activePath"],
            "isModified": False,
            "isPersisted": True,
            "isTemporary": False,
        }
        exported["graphStats"] = {
            "uiNodeCount": 1,
            "uiLinkCount": 0,
            "apiNodeCount": 2,
        }
        draft = materialize_workspace_export(
            exported,
            PARAMETERIZATION,
            OPERATION_REF,
            "first-last",
        )
        self.assertEqual(draft.manifest["source"]["schema"], "comfy.workspace-export/2")
        self.assertEqual(draft.manifest["source"]["workspace_control_version"], "0.4.0")

    def test_materialized_draft_writes_four_guarded_products(self):
        draft = materialize_workspace_export(
            WORKSPACE_EXPORT,
            PARAMETERIZATION,
            OPERATION_REF,
            "first-last",
        )
        self.assertEqual(draft.manifest["state"], "offline-draft")
        self.assertIsNone(draft.manifest["runtime_manifest"])
        with tempfile.TemporaryDirectory() as directory:
            paths = write_materialized_draft(directory, draft)
            self.assertEqual(len(paths), 4)
            self.assertTrue(all(Path(path).is_file() for path in paths.values()))
            with self.assertRaisesRegex(FileExistsError, "refusing to replace"):
                write_materialized_draft(directory, draft)

    def test_materialization_rejects_tampering_stale_values_and_links(self):
        tampered = dict(WORKSPACE_EXPORT)
        tampered["apiGraphSignature"] = "0" * 64
        with self.assertRaisesRegex(GraphValidationError, "does not match"):
            materialize_workspace_export(
                tampered,
                PARAMETERIZATION,
                OPERATION_REF,
                "first-last",
            )
        stale = json.loads(json.dumps(PARAMETERIZATION))
        stale["parameters"][0]["expected"] = "other.png"
        with self.assertRaisesRegex(GraphValidationError, "does not match expected"):
            parameterize_api_graph(GRAPH, stale)
        link = {
            "schema": "comfy.api-parameterization/1",
            "parameters": [
                {
                    "name": "images_link",
                    "pointers": ["/2/inputs/images"],
                    "expected": ["1", 0],
                }
            ],
        }
        with self.assertRaisesRegex(GraphValidationError, "cannot parameterize a graph link"):
            parameterize_api_graph(GRAPH, link)

    def test_graph_validation_rejects_missing_link_and_enum(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "not-listed.png"}},
            "2": {
                "class_type": "SaveImage",
                "inputs": {"images": ["404", 0], "filename_prefix": "x"},
            },
        }
        report = validate_api_graph(graph, OBJECT_INFO)
        self.assertFalse(report["valid"])
        self.assertEqual({item["code"] for item in report["issues"]}, {"enum_value", "missing_link_source"})

    def test_submit_wait_artifact_and_receipt(self):
        client, _ = self.client()
        submitted = submit_graph(client, GRAPH)
        history = wait_for_job(client, submitted.prompt_id, timeout=1.0, interval=0.001)
        artifacts = artifacts_from_history(history)
        self.assertEqual(artifacts[0].filename, "x.png")
        manifest = probe_runtime(client)
        receipt = build_run_receipt(
            operation_ref={
                "id": "generate.keyframed",
                "version": 1,
                "contract_hash": "a" * 64,
            },
            api_graph=GRAPH,
            runtime_manifest=manifest,
            prompt_id=submitted.prompt_id,
            history=history,
            artifacts=artifacts,
            evidence_status="executes",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = save_receipt(Path(directory) / "p1.json", receipt)
            self.assertEqual(json.loads(path.read_text())["receipt_hash"], receipt["receipt_hash"])
        self.assertEqual(receipt["schema"], "comfy.run-receipt/2")
        self.assertEqual(receipt["operation"], "generate.keyframed")

    def test_receipt_requires_content_addressed_operation(self):
        client, _ = self.client()
        manifest = probe_runtime(client)
        with self.assertRaisesRegex(ValueError, "operation_ref"):
            build_run_receipt(
                operation_ref={"id": "invalid"},
                api_graph=GRAPH,
                runtime_manifest=manifest,
                prompt_id="p1",
                history=None,
                artifacts=[],
                evidence_status="executes",
            )

    def test_manager_guard_requires_exact_target(self):
        client, transport = self.client()
        plan = plan_custom_node_update(
            "ComfyUI-Cauce",
            source_url="https://github.com/hypereikon-lab/ComfyUI-Cauce",
        )
        with self.assertRaises(MutationGuardError):
            apply_mutation(client, plan, confirmation="all")
        response = apply_mutation(client, plan, confirmation="ComfyUI-Cauce")
        self.assertEqual(response, {"ok": True})
        manager_paths = [call[1].split("https://unit.invalid", 1)[-1] for call in transport.calls]
        self.assertEqual(
            manager_paths,
            ["/manager/queue/reset", "/manager/queue/update", "/manager/queue/start"],
        )

    def test_unknown_update_requires_exact_public_source_and_install_guard(self):
        client, transport = self.client()
        with self.assertRaisesRegex(ValueError, "source_url"):
            plan_custom_node_update("ComfyUI-Cauce")
        with self.assertRaisesRegex(ValueError, "public HTTPS GitHub"):
            plan_custom_node_update("x", source_url="https://example.com/x")
        with self.assertRaisesRegex(ValueError, "must use version unknown"):
            plan_custom_node_update(
                "ComfyUI-Cauce",
                version="dcb48570b362cbbdcb9d5b739c6b1c0ca278fa40",
                source_url="https://github.com/hypereikon-lab/ComfyUI-Cauce",
            )
        source = "https://github.com/owner/repository"
        plan = plan_git_install(
            source,
            visibility_confirmation="public",
            default_branch="main",
            recovery_channel="external-operator",
        )
        with self.assertRaises(MutationGuardError):
            install_git_url(
                client,
                plan,
                confirmation="https://github.com/owner/other",
                journal_path="unused.json",
            )
        with tempfile.TemporaryDirectory() as directory:
            result = install_git_url(
                client,
                plan,
                confirmation=source,
                journal_path=Path(directory) / "install.json",
            )
        self.assertEqual(result["state"], "acknowledged")
        self.assertEqual(result["result"], "installed")
        install_call = transport.calls[-1]
        self.assertEqual(install_call[3], {"url": source})
        self.assertIsNone(install_call[4])

    def test_git_install_falls_back_to_legacy_text_body_only_after_400(self):
        transport = LegacyGitInstallTransport()
        client = ComfyClient(RuntimeConfig("https://unit.invalid"), transport)
        source = "https://github.com/owner/repository"
        plan = plan_git_install(
            source,
            visibility_confirmation="public",
            default_branch="main",
            recovery_channel="external-operator",
        )
        with tempfile.TemporaryDirectory() as directory:
            result = install_git_url(
                client,
                plan,
                confirmation=source,
                journal_path=Path(directory) / "install.json",
            )
        self.assertEqual(result["result"], "installed")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][3], {"url": source})
        self.assertEqual(transport.calls[1][4], source.encode("utf-8"))

    def test_git_install_requires_public_visibility_and_independent_recovery(self):
        source = "https://github.com/owner/repository"
        with self.assertRaisesRegex(MutationGuardError, "public"):
            plan_git_install(
                source,
                visibility_confirmation="private",
                default_branch="main",
                recovery_channel="external-operator",
            )
        with self.assertRaisesRegex(MutationGuardError, "recovery_channel"):
            plan_git_install(
                source,
                visibility_confirmation="public",
                default_branch="main",
                recovery_channel="same-origin-manager",
            )

    def test_git_install_persists_unknown_outcome_and_forbids_retry(self):
        transport = UnknownGitInstallTransport()
        client = ComfyClient(RuntimeConfig("https://unit.invalid"), transport)
        source = "https://github.com/owner/repository"
        plan = plan_git_install(
            source,
            visibility_confirmation="public",
            default_branch="main",
            recovery_channel="external-operator",
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "install.json"
            with self.assertRaisesRegex(MutationGuardError, "outcome is unknown"):
                install_git_url(
                    client,
                    plan,
                    confirmation=source,
                    journal_path=journal,
                )
            record = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "outcome-unknown")
            self.assertEqual(record["attempt_count"], 1)
            with self.assertRaisesRegex(MutationGuardError, "reconcile"):
                install_git_url(
                    client,
                    plan,
                    confirmation=source,
                    journal_path=journal,
                )

    def test_git_install_reconciliation_records_inventory_evidence(self):
        client, _ = self.client()
        source = "https://github.com/owner/repository"
        plan = plan_git_install(
            source,
            visibility_confirmation="public",
            default_branch="main",
            recovery_channel="external-operator",
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "install.json"
            install_git_url(client, plan, confirmation=source, journal_path=journal)
            result = reconcile_git_install(client, journal)
        self.assertEqual(result["state"], "reconciled-installed")
        self.assertFalse(
            result["reconciliation"]["manual_partial_directory_check_required"]
        )
        self.assertFalse(result["reconciliation"]["retry_authorized"])

    def test_runtime_config_requires_complete_service_token(self):
        with self.assertRaises(ValueError):
            RuntimeConfig("https://unit.invalid", access_client_id="only-id")
        with self.assertRaisesRegex(ValueError, "single-line"):
            RuntimeConfig("https://unit.invalid", user_agent="invalid\nheader")

    def test_media_routes_reject_traversal_and_unbounded_types(self):
        client, _ = self.client()
        with self.assertRaisesRegex(ValueError, "media root"):
            client.view_artifact("x.png", "../outside", "output")
        with self.assertRaisesRegex(ValueError, "directory"):
            client.artifact_url("nested/x.png", "", "output")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "x.png"
            source.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "input or temp"):
                client.upload_image(source, upload_type="output")

    def _series_fixture(self, directory, *, step_count=2):
        root = Path(directory)
        (root / "graphs").mkdir()
        (root / "operations").mkdir()
        steps = []
        previous = None
        for number in range(1, step_count + 1):
            step_id = f"step-{number}"
            graph = root / "graphs" / f"{step_id}.json"
            operation = root / "operations" / f"{step_id}.json"
            graph.write_text(json.dumps(GRAPH), encoding="utf-8")
            operation.write_text(
                json.dumps(
                    {
                        "id": "continue.native_av",
                        "version": 2,
                        "contract_hash": f"{number:x}" * 64,
                    }
                ),
                encoding="utf-8",
            )
            steps.append(
                {
                    "id": step_id,
                    "graph": f"graphs/{step_id}.json",
                    "operation_ref": f"operations/{step_id}.json",
                    "depends_on": previous,
                }
            )
            previous = step_id
        return {
            "schema": SERIES_SCHEMA,
            "id": "unit-series",
            "steps": steps,
        }

    def test_series_plan_requires_exact_serial_dependencies_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._series_fixture(directory)
            steps, plan_hash = validate_series_plan(plan, root=Path(directory))
            self.assertEqual([step.id for step in steps], ["step-1", "step-2"])
            self.assertEqual(len(plan_hash), 64)
            broken = json.loads(json.dumps(plan))
            broken["steps"][1]["depends_on"] = None
            with self.assertRaisesRegex(ValueError, "immediately preceding"):
                validate_series_plan(broken, root=Path(directory))
            unsafe = json.loads(json.dumps(plan))
            unsafe["steps"][0]["graph"] = "../outside.json"
            with self.assertRaisesRegex(ValueError, "safe relative"):
                validate_series_plan(unsafe, root=Path(directory))

    def test_series_executes_strictly_serially_and_persists_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._series_fixture(directory)
            transport = SeriesTransport()
            client = ComfyClient(RuntimeConfig("https://unit.invalid"), transport)
            result = run_series(
                client,
                plan,
                plan_root=root,
                state_path=root / "state.json",
                receipts_dir=root / "receipts",
                timeout=1.0,
                interval=0.001,
            )
            self.assertEqual(result["completed_steps"], ["step-1", "step-2"])
            self.assertEqual(result["prompt_ids"], ["p1", "p2"])
            state = json.loads((root / "state.json").read_text())
            steps, plan_hash = validate_series_plan(plan, root=root)
            validate_series_state(
                state,
                plan_id=plan["id"],
                plan_hash=plan_hash,
                steps=steps,
            )
            self.assertTrue(all(Path(path).is_file() for path in result["receipts"]))
            prompt_calls = [call for call in transport.calls if call[1].endswith("/prompt")]
            self.assertEqual(len(prompt_calls), 2)
            self.assertEqual(
                prompt_calls[1][3]["extra_data"]["runtime_control"]["series_step"],
                "step-2",
            )

    def test_series_resume_polls_stored_prompt_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self._series_fixture(directory, step_count=1)
            steps, plan_hash = validate_series_plan(plan, root=root)
            state = {
                "schema": STATE_SCHEMA,
                "plan_id": plan["id"],
                "plan_hash": plan_hash,
                "steps": [
                    {
                        "id": "step-1",
                        "status": "submitted",
                        "prompt_id": "resume-77",
                        "receipt": None,
                        "receipt_hash": None,
                    }
                ],
            }
            state["state_hash"] = content_hash(state)
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            transport = SeriesTransport(resumed_prompt_id="resume-77")
            client = ComfyClient(RuntimeConfig("https://unit.invalid"), transport)
            result = run_series(
                client,
                plan,
                plan_root=root,
                state_path=state_path,
                receipts_dir=root / "receipts",
                timeout=1.0,
                interval=0.001,
            )
            self.assertEqual(result["prompt_ids"], ["resume-77"])
            self.assertFalse(any(call[1].endswith("/prompt") for call in transport.calls))
            tampered = json.loads(state_path.read_text())
            tampered["steps"][0]["prompt_id"] = "other"
            with self.assertRaisesRegex(ValueError, "state hash"):
                validate_series_state(
                    tampered,
                    plan_id=plan["id"],
                    plan_hash=plan_hash,
                    steps=steps,
                )


if __name__ == "__main__":
    unittest.main()

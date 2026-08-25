from pathlib import Path
import json
import tempfile
import unittest

from comfy_runtime_control.artifacts import artifacts_from_history
from comfy_runtime_control.client import ComfyClient, RuntimeConfig
from comfy_runtime_control.compiler import compile_api_template
from comfy_runtime_control.errors import GraphValidationError, MutationGuardError
from comfy_runtime_control.jobs import submit_graph, wait_for_job
from comfy_runtime_control.manager import apply_mutation, install_git_url, plan_custom_node_update
from comfy_runtime_control.probe import probe_runtime, public_manifest
from comfy_runtime_control.receipts import build_run_receipt, save_receipt
from comfy_runtime_control.schema import dependency_plan, validate_api_graph


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
            value = {"system": {"comfyui_version": "test"}, "devices": []}
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

    def test_graph_validation_and_dependency_plan(self):
        report = validate_api_graph(GRAPH, OBJECT_INFO)
        self.assertTrue(report["valid"])
        self.assertTrue(dependency_plan(GRAPH, OBJECT_INFO)["ready"])
        broken = dict(GRAPH)
        broken["3"] = {"class_type": "MissingNode", "inputs": {}}
        report = validate_api_graph(broken, OBJECT_INFO)
        self.assertFalse(report["valid"])
        self.assertIn("MissingNode", dependency_plan(broken, OBJECT_INFO)["missing_node_types"])

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
        source = "https://github.com/owner/repository"
        with self.assertRaises(MutationGuardError):
            install_git_url(client, source, confirmation="https://github.com/owner/other")
        self.assertEqual(install_git_url(client, source, confirmation=source), "installed")
        install_call = transport.calls[-1]
        self.assertEqual(install_call[3], {"url": source})
        self.assertIsNone(install_call[4])

    def test_git_install_falls_back_to_legacy_text_body_only_after_400(self):
        transport = LegacyGitInstallTransport()
        client = ComfyClient(RuntimeConfig("https://unit.invalid"), transport)
        source = "https://github.com/owner/repository"
        self.assertEqual(install_git_url(client, source, confirmation=source), "installed")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][3], {"url": source})
        self.assertEqual(transport.calls[1][4], source.encode("utf-8"))

    def test_runtime_config_requires_complete_service_token(self):
        with self.assertRaises(ValueError):
            RuntimeConfig("https://unit.invalid", access_client_id="only-id")

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


if __name__ == "__main__":
    unittest.main()

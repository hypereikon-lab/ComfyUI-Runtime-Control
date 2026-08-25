"""Bounded client for documented and introspected ComfyUI HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import uuid
from typing import Any
from urllib.parse import quote, urljoin

from .errors import ComfyRuntimeError
from .transport import Transport, UrlLibTransport, with_query


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    timeout: float = 30.0
    client_id: str = ""
    access_client_id: str | None = None
    access_client_secret: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        if bool(self.access_client_id) != bool(self.access_client_secret):
            raise ValueError("both Cloudflare Access service-token values are required")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")


class ComfyClient:
    def __init__(self, config: RuntimeConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or UrlLibTransport()
        self.client_id = config.client_id or str(uuid.uuid4())

    @property
    def safe_runtime_label(self) -> str:
        return self.config.base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.access_client_id and self.config.access_client_secret:
            headers["CF-Access-Client-Id"] = self.config.access_client_id
            headers["CF-Access-Client-Secret"] = self.config.access_client_secret
        return headers

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], bytes]:
        url = with_query(self._url(path), query or {})
        request_headers = self._headers()
        request_headers.update(headers or {})
        status, response_headers, body = self.transport.request(
            method,
            url,
            headers=request_headers,
            json_body=json_body,
            data=data,
            timeout=self.config.timeout,
        )
        if not 200 <= status < 300:
            raise ComfyRuntimeError(f"unexpected HTTP {status} for {method} {path}")
        return response_headers, body

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        _, body = self.request_bytes(method, path, query=query, json_body=json_body)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyRuntimeError(f"{path} did not return JSON") from exc

    def get(self, path: str, *, query: dict[str, str] | None = None) -> Any:
        return self.request_json("GET", path, query=query)

    def post(self, path: str, body: Any | None = None) -> Any:
        return self.request_json("POST", path, json_body=body)

    def upload_image(
        self,
        source: str | Path,
        *,
        subfolder: str = "",
        overwrite: bool = False,
        upload_type: str = "input",
    ) -> Any:
        source_path = Path(source)
        if not source_path.is_file():
            raise ValueError(f"source file does not exist: {source_path}")
        boundary = f"----comfy-runtime-{uuid.uuid4().hex}"
        fields = {
            "type": upload_type,
            "subfolder": subfolder,
            "overwrite": "true" if overwrite else "false",
        }
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )
        filename = source_path.name.replace('"', "_")
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="image"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                source_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        _, response = self.request_bytes(
            "POST",
            "/upload/image",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            return json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyRuntimeError("upload did not return JSON") from exc

    def view_artifact(self, filename: str, subfolder: str, artifact_type: str) -> bytes:
        _, body = self.request_bytes(
            "GET",
            "/view",
            query={"filename": filename, "subfolder": subfolder, "type": artifact_type},
        )
        return body

    def artifact_url(self, filename: str, subfolder: str, artifact_type: str) -> str:
        query = (
            f"filename={quote(filename)}&subfolder={quote(subfolder)}&type={quote(artifact_type)}"
        )
        return f"{self._url('/view')}?{query}"

"""Bounded client for documented and introspected ComfyUI HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
import uuid
from urllib.parse import quote, urljoin

from .errors import ComfyRuntimeError
from .transport import Transport, UrlLibTransport, with_query
from .version import __version__


DEFAULT_USER_AGENT = f"comfy-runtime-control/{__version__}"


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    timeout: float = 30.0
    client_id: str = ""
    access_client_id: str | None = None
    access_client_secret: str | None = None
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        if bool(self.access_client_id) != bool(self.access_client_secret):
            raise ValueError("both Cloudflare Access service-token values are required")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if not self.user_agent or "\r" in self.user_agent or "\n" in self.user_agent:
            raise ValueError("user_agent must be a non-empty single-line value")


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
        headers = {
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
        }
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

    def request_into(
        self,
        method: str,
        path: str,
        destination: BinaryIO,
        *,
        query: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], int]:
        """Stream a response when supported, with a bounded in-memory fallback."""
        url = with_query(self._url(path), query or {})
        request_into = getattr(self.transport, "request_into", None)
        if callable(request_into):
            status, response_headers, written = request_into(
                method,
                url,
                destination,
                headers=self._headers(),
                timeout=self.config.timeout,
            )
            if not 200 <= status < 300:
                raise ComfyRuntimeError(f"unexpected HTTP {status} for {method} {path}")
            return response_headers, written
        response_headers, body = self.request_bytes(method, path, query=query)
        destination.write(body)
        return response_headers, len(body)

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
        if upload_type not in {"input", "temp"}:
            raise ValueError("upload_type must be input or temp")
        _safe_relative_path(subfolder, "subfolder", allow_empty=True)
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
        if artifact_type not in {"input", "output", "temp"}:
            raise ValueError("artifact_type must be input, output, or temp")
        _safe_relative_path(subfolder, "subfolder", allow_empty=True)
        _safe_relative_path(filename, "filename", allow_empty=False, require_basename=True)
        _, body = self.request_bytes(
            "GET",
            "/view",
            query={"filename": filename, "subfolder": subfolder, "type": artifact_type},
        )
        return body

    def stream_artifact(
        self,
        filename: str,
        subfolder: str,
        artifact_type: str,
        destination: BinaryIO,
    ) -> tuple[dict[str, str], int]:
        if artifact_type not in {"input", "output", "temp"}:
            raise ValueError("artifact_type must be input, output, or temp")
        _safe_relative_path(subfolder, "subfolder", allow_empty=True)
        _safe_relative_path(filename, "filename", allow_empty=False, require_basename=True)
        return self.request_into(
            "GET",
            "/view",
            destination,
            query={"filename": filename, "subfolder": subfolder, "type": artifact_type},
        )

    def artifact_url(self, filename: str, subfolder: str, artifact_type: str) -> str:
        if artifact_type not in {"input", "output", "temp"}:
            raise ValueError("artifact_type must be input, output, or temp")
        _safe_relative_path(subfolder, "subfolder", allow_empty=True)
        _safe_relative_path(filename, "filename", allow_empty=False, require_basename=True)
        query = (
            f"filename={quote(filename)}&subfolder={quote(subfolder)}&type={quote(artifact_type)}"
        )
        return f"{self._url('/view')}?{query}"


def _safe_relative_path(
    value: str,
    name: str,
    *,
    allow_empty: bool,
    require_basename: bool = False,
) -> PurePosixPath:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must remain inside its ComfyUI media root")
    if require_basename and len(path.parts) != 1:
        raise ValueError(f"{name} must not contain a directory")
    return path

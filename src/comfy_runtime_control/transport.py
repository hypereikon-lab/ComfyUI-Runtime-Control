"""Small standard-library HTTP transport with injectable test doubles."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import ComfyRuntimeError


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]: ...


class UrlLibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        if json_body is not None:
            if data is not None:
                raise ValueError("json_body and data are mutually exclusive")
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ComfyRuntimeError(f"HTTP {exc.code} for {method} {url}: {body[:500]}") from exc
        except URLError as exc:
            raise ComfyRuntimeError(f"request failed for {method} {url}: {exc.reason}") from exc


def with_query(url: str, values: dict[str, str]) -> str:
    return f"{url}?{urlencode(values)}" if values else url

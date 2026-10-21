"""HTTP layer: Site Manager (api.ui.com) and the Network Integration API.

The Network API is reached either through the Site Manager Connector
(``/v1/connector/consoles/{id}/network/integration``) or directly on a local
console (``https://<console>/proxy/network/integration``); both speak the same
paths, so one client serves both.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

RETRY_AFTER_RE = re.compile(r"retry after ([\d.]+)s")
MAX_RETRIES = 2
NETWORK_PAGE_SIZE = 200
SM_PAGE_SIZE = 100
NETWORK_APP = "network/integration"
PROTECT_APP = "protect/integration"
PROTECT_INTERNAL_APP = "protect/api"


class UniFiAPIError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None, url: str | None = None):
        self.status = status
        self.code = code
        self.url = url
        super().__init__(f"HTTP {status}{f' {code}' if code else ''}: {message}")


@dataclass(frozen=True)
class NetworkTarget:
    """Where a console's Network Integration API lives and how to authenticate."""

    label: str
    root: str  # .../v1/connector/consoles/{id} (cloud) or https://<console>/proxy (local)
    api_key: str
    via: str  # "cloud" or "local"
    verify: bool | str = True
    host_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"{self.root}/{NETWORK_APP}"

    def url(self, app: str, path: str) -> str:
        return f"{self.root}/{app}{path}"

    @property
    def limiter_key(self) -> str:
        return self.host_id or self.root


class RateLimiter:
    """Sliding-window limiter per key (the Connector allows 100 req/min per console)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, key: str) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hits = self._hits.setdefault(key, deque())
            while True:
                now = time.monotonic()
                while hits and now - hits[0] >= 60:
                    hits.popleft()
                if len(hits) < self.per_minute:
                    hits.append(now)
                    return
                await asyncio.sleep(60 - (now - hits[0]) + 0.05)


def _error_from_response(resp: httpx.Response) -> UniFiAPIError:
    message, code = resp.reason_phrase, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or message
        code = body.get("code") or body.get("statusName")
    elif resp.text:
        message = resp.text[:500]
    return UniFiAPIError(resp.status_code, str(message), code, str(resp.request.url))


def _retry_delay(resp: httpx.Response) -> float:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = RETRY_AFTER_RE.search(resp.text or "")
    return float(match.group(1)) if match else 5.0


class HttpBackend:
    def __init__(self, timeout: float, rate_limit_per_minute: int, transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self.limiter = RateLimiter(rate_limit_per_minute)
        self._transport = transport
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, verify: bool | str) -> httpx.AsyncClient:
        key = str(verify)
        if key not in self._clients:
            self._clients[key] = httpx.AsyncClient(timeout=self.timeout, verify=verify, transport=self._transport)
        return self._clients[key]

    async def request(
        self,
        method: str,
        url: str,
        api_key: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        verify: bool | str = True,
        limiter_key: str | None = None,
        raw: bool = False,
    ) -> Any:
        headers = {"X-API-Key": api_key, "Accept": "*/*" if raw else "application/json"}
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(MAX_RETRIES + 1):
            if limiter_key:
                await self.limiter.acquire(limiter_key)
            resp = await self._client(verify).request(method, url, headers=headers, params=params, json=json)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                await asyncio.sleep(min(_retry_delay(resp), 30))
                continue
            if resp.status_code >= 400:
                raise _error_from_response(resp)
            if raw:
                return resp.content, resp.headers.get("content-type", "")
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise UniFiAPIError(429, "rate limited")  # pragma: no cover

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


class SiteManagerAPI:
    def __init__(self, backend: HttpBackend, api_key: str | None, base_url: str):
        self.backend = backend
        self.api_key = api_key
        self.base_url = base_url

    def _key(self) -> str:
        if not self.api_key:
            raise UniFiAPIError(401, "UNIFI_SM_API_KEY is not set; create a key at unifi.ui.com > API")
        return self.api_key

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.backend.request("GET", f"{self.base_url}{path}", self._key(), params=params)

    async def post(self, path: str, body: Any) -> Any:
        return await self.backend.request("POST", f"{self.base_url}{path}", self._key(), json=body)

    async def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Follow nextToken pagination."""
        params = dict(params or {})
        params.setdefault("pageSize", SM_PAGE_SIZE)
        items: list[Any] = []
        while True:
            page = await self.get(path, params)
            items.extend(page.get("data") or [])
            token = page.get("nextToken")
            if not token:
                return items
            params["nextToken"] = token


class NetworkAPI:
    """Console-side APIs (Network, and Protect via ``app=``) on a resolved target."""

    def __init__(self, backend: HttpBackend):
        self.backend = backend

    async def request(
        self,
        target: NetworkTarget,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        app: str = NETWORK_APP,
        raw: bool = False,
    ) -> Any:
        return await self.backend.request(
            method.upper(),
            target.url(app, path),
            target.api_key,
            params=params,
            json=body,
            verify=target.verify,
            limiter_key=target.limiter_key,
            raw=raw,
        )

    async def get_page(
        self,
        target: NetworkTarget,
        path: str,
        *,
        limit: int = 100,
        offset: int = 0,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch up to ``limit`` items starting at ``offset``, following offset pagination."""
        items: list[Any] = []
        total = None
        cursor = offset
        while len(items) < limit:
            page_size = min(NETWORK_PAGE_SIZE, limit - len(items))
            page = await self.request(
                target, "GET", path, params={**(params or {}), "offset": cursor, "limit": page_size}
            )
            if not isinstance(page, dict) or "data" not in page:
                return {"data": page}
            data = page.get("data") or []
            items.extend(data)
            total = page.get("totalCount", total)
            cursor += len(data)
            if not data or (total is not None and cursor >= total):
                break
        return {"totalCount": total, "offset": offset, "count": len(items), "data": items}

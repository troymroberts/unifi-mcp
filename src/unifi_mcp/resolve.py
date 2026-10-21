"""Turn the human-friendly console/site names the tools accept into API targets."""

from __future__ import annotations

import os
import time
from typing import Any

from .client import NetworkAPI, NetworkTarget, SiteManagerAPI
from .config import ConsoleAlias, Settings

CACHE_TTL = 300


class ResolveError(Exception):
    pass


def host_name(host: dict[str, Any]) -> str:
    state = host.get("reportedState") or {}
    return state.get("hostname") or state.get("name") or host.get("id", "?")


def _match(query: str, candidates: list[tuple[str, Any]], kind: str) -> Any:
    """Exact (case-insensitive) match on any key, else a unique substring match."""
    q = query.strip().lower()
    exact = [obj for key, obj in candidates if key.lower() == q]
    if exact:
        unique = {id(o): o for o in exact}
        if len(unique) == 1:
            return exact[0]
        raise ResolveError(f"'{query}' matches several {kind}s; use the id")
    partial = {id(obj): (key, obj) for key, obj in candidates if q in key.lower()}
    if len(partial) == 1:
        return next(iter(partial.values()))[1]
    if partial:
        names = sorted({key for key, _ in partial.values()})[:15]
        raise ResolveError(f"'{query}' is ambiguous among {kind}s: {', '.join(names)}")
    raise ResolveError(f"no {kind} matches '{query}'")


class Resolver:
    def __init__(self, settings: Settings, site_manager: SiteManagerAPI, network: NetworkAPI):
        self.settings = settings
        self.sm = site_manager
        self.network = network
        self._hosts: tuple[float, list[dict[str, Any]]] | None = None
        self._sites: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def hosts(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or not self._hosts or time.monotonic() - self._hosts[0] > CACHE_TTL:
            self._hosts = (time.monotonic(), await self.sm.get_all("/v1/hosts"))
        return self._hosts[1]

    async def find_host(self, query: str) -> dict[str, Any]:
        hosts = await self.hosts()
        candidates = [(h["id"], h) for h in hosts] + [(host_name(h), h) for h in hosts]
        return _match(query, candidates, "console")

    def _local_target(self, alias: ConsoleAlias) -> NetworkTarget:
        key = os.environ.get(alias.api_key_env or "")
        if not key:
            raise ResolveError(f"console '{alias.name}': environment variable {alias.api_key_env} is not set")
        return NetworkTarget(
            label=alias.name,
            root=f"{alias.local_url}/proxy",
            api_key=key,
            via="local",
            verify=alias.verify_ssl,
        )

    def _cloud_target(self, host: dict[str, Any]) -> NetworkTarget:
        if not self.settings.sm_api_key:
            raise ResolveError("UNIFI_SM_API_KEY is not set; cloud consoles need a Site Manager API key")
        state = (host.get("reportedState") or {}).get("state")
        if state and state != "connected":
            raise ResolveError(f"console '{host_name(host)}' is {state}; the Connector only reaches connected consoles")
        return NetworkTarget(
            label=host_name(host),
            root=f"{self.settings.cloud_base_url}/v1/connector/consoles/{host['id']}",
            api_key=self.settings.sm_api_key,
            via="cloud",
            host_id=host["id"],
        )

    async def console(self, query: str | None) -> NetworkTarget:
        query = query or self.settings.default_console
        if not query:
            raise ResolveError("no console given and no default set; call list_consoles to see names")
        alias = self.settings.aliases.get(query.strip().lower())
        if alias and alias.is_local:
            return self._local_target(alias)
        host = await self.find_host(alias.host if alias else query)
        return self._cloud_target(host)

    async def sites(self, target: NetworkTarget, refresh: bool = False) -> list[dict[str, Any]]:
        cached = self._sites.get(target.base_url)
        if refresh or not cached or time.monotonic() - cached[0] > CACHE_TTL:
            page = await self.network.get_page(target, "/v1/sites", limit=1000)
            cached = (time.monotonic(), page["data"])
            self._sites[target.base_url] = cached
        return cached[1]

    async def site_id(self, target: NetworkTarget, query: str | None) -> str:
        sites = await self.sites(target)
        if not query:
            if len(sites) == 1:
                return sites[0]["id"]
            names = ", ".join(s.get("name", s["id"]) for s in sites)
            raise ResolveError(f"console '{target.label}' has {len(sites)} sites; pass site= one of: {names}")
        candidates = []
        for s in sites:
            candidates += [(s["id"], s), (s.get("name") or "", s), (s.get("internalReference") or "", s)]
        return _match(query, candidates, "site")["id"]

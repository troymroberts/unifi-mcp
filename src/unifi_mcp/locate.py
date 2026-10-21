"""find_client: locate a client (by MAC, IP or name) on a console/site, down to the switch port or AP.

The official Network Integration API gives a client's uplink device but not the switch port.
The classic controller API (``stat/sta``) has both, and the api.ui.com Connector proxies it with
the same Site Manager key, so it is tried first; the Integration API is the fallback.

Search order: the caller's hint (console/site, e.g. the last-seen site from an inventory),
locations remembered this session, consoles whose ``groups`` list the given group, then every
reachable console in the fleet.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget, UniFiAPIError
from .config import Settings
from .resolve import ResolveError, Resolver

MAC_RE = re.compile(r"^[0-9a-f]{12}$")
FLEET_CONCURRENCY = 4


def normalize_mac(value: str | None) -> str | None:
    """'AA-BB-CC-DD-EE-FF', 'aabb.ccdd.eeff', 'aa:bb:...' -> 'aa:bb:cc:dd:ee:ff'; None if not a MAC."""
    if not value:
        return None
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if not MAC_RE.match(hexdigits):
        return None
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


class ClientLocator:
    def __init__(self, settings: Settings, resolver: Resolver, network: NetworkAPI):
        self.settings = settings
        self.resolver = resolver
        self.network = network
        self.remembered: dict[str, tuple[str, str]] = {}  # mac -> (console query, site reference)

    # ------------------------------------------------------------------ data per site

    async def _legacy(self, target: NetworkTarget, site_ref: str, path: str) -> list[dict[str, Any]]:
        body = await self.network.request(target, "GET", f"/s/{site_ref}{path}", app=NETWORK_LEGACY_APP)
        return body.get("data", []) if isinstance(body, dict) else []

    async def _site_clients(self, target: NetworkTarget, site: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """All connected clients on a site, as (records, source). Legacy first (has switch ports)."""
        try:
            rows = await self._legacy(target, site["internalReference"], "/stat/sta")
            return [self._from_legacy(r) for r in rows], "legacy stat/sta"
        except UniFiAPIError:
            page = await self.network.get_page(target, f"/v1/sites/{site['id']}/clients", limit=5000)
            return [self._from_integration(r) for r in page.get("data", [])], "integration /clients"

    async def _site_devices(self, target: NetworkTarget, site: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """UniFi devices on a site keyed by MAC and by id: name, model, type."""
        out: dict[str, dict[str, Any]] = {}
        try:
            for d in await self._legacy(target, site["internalReference"], "/stat/device-basic"):
                mac = normalize_mac(d.get("mac"))
                if mac:
                    out[mac] = {"name": d.get("name") or mac, "model": d.get("model"), "type": d.get("type")}
            if out:
                return out
        except UniFiAPIError:
            pass
        page = await self.network.get_page(target, f"/v1/sites/{site['id']}/devices", limit=5000)
        for d in page.get("data", []):
            info = {"name": d.get("name"), "model": d.get("model"), "type": ",".join(d.get("features") or [])}
            mac = normalize_mac(d.get("macAddress"))
            if mac:
                out[mac] = info
            if d.get("id"):
                out[d["id"]] = {**info, "mac": mac}
        return out

    @staticmethod
    def _from_legacy(r: dict[str, Any]) -> dict[str, Any]:
        wired = bool(r.get("is_wired"))
        return {
            "mac": normalize_mac(r.get("mac")),
            "ip": r.get("ip") or r.get("last_ip"),
            "name": r.get("name") or r.get("hostname"),
            "hostname": r.get("hostname"),
            "connection": "wired" if wired else "wireless",
            "network": r.get("network"),
            "vlan": r.get("vlan"),
            "switch_mac": normalize_mac(r.get("sw_mac")) if wired else None,
            "switch_port": r.get("sw_port") if wired else None,
            "switch_depth": r.get("sw_depth") if wired else None,
            "ap_mac": normalize_mac(r.get("ap_mac")) if not wired else None,
            "ssid": r.get("essid") if not wired else None,
            "last_seen": r.get("last_seen"),
            "uptime_s": r.get("uptime"),
        }

    @staticmethod
    def _from_integration(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "mac": normalize_mac(r.get("macAddress")),
            "ip": r.get("ipAddress"),
            "name": r.get("name"),
            "hostname": None,
            "connection": {"WIRED": "wired", "WIRELESS": "wireless"}.get(r.get("type"), (r.get("type") or "").lower()),
            "uplink_device_id": r.get("uplinkDeviceId"),
            "connected_at": r.get("connectedAt"),
        }

    @staticmethod
    def _matches(c: dict[str, Any], macs: set[str], ip: str | None, name: str | None) -> str | None:
        if c.get("mac") and c["mac"] in macs:
            return "mac"
        if ip and c.get("ip") == ip:
            return "ip"
        if name:
            n = name.strip().lower()
            if n and n in {str(c.get("name") or "").lower(), str(c.get("hostname") or "").lower()}:
                return "name"
        return None

    async def search_site(self, target: NetworkTarget, site: dict[str, Any], macs: set[str],
                          ip: str | None, name: str | None) -> list[dict[str, Any]]:
        clients, source = await self._site_clients(target, site)
        hits = [(c, how) for c in clients if (how := self._matches(c, macs, ip, name))]
        if not hits:
            return []
        devices = await self._site_devices(target, site)
        results = []
        for c, how in hits:
            entry = {
                "console": target.label, "via": target.via, "site": site.get("name"),
                "site_ref": site.get("internalReference"), "matched_by": how, "source": source,
                "client": {k: c[k] for k in ("mac", "ip", "name", "hostname", "connection", "network", "vlan",
                                             "last_seen", "uptime_s", "connected_at") if c.get(k) is not None},
            }
            if c.get("switch_mac"):
                sw = devices.get(c["switch_mac"], {})
                entry["switch"] = {"name": sw.get("name"), "model": sw.get("model"), "mac": c["switch_mac"],
                                   "port": c.get("switch_port"), "depth": c.get("switch_depth")}
            if c.get("ap_mac"):
                ap = devices.get(c["ap_mac"], {})
                entry["access_point"] = {"name": ap.get("name"), "model": ap.get("model"), "mac": c["ap_mac"],
                                         "ssid": c.get("ssid")}
            if c.get("uplink_device_id"):
                up = devices.get(c["uplink_device_id"], {})
                key = "switch" if c.get("connection") == "wired" else "access_point"
                entry[key] = {"name": up.get("name"), "model": up.get("model"), "mac": up.get("mac"),
                              "port": None, "note": "Integration API fallback: no switch port available"}
            results.append(entry)
            if c.get("mac"):
                self.remembered[c["mac"]] = (target.host_id or target.label, site.get("internalReference"))
        return results

    # ------------------------------------------------------------------ search plan

    async def _sites(self, target: NetworkTarget, site: str | None) -> list[dict[str, Any]]:
        sites = await self.resolver.sites(target)
        if not site:
            return sites
        site_id = await self.resolver.site_id(target, site)
        return [s for s in sites if s["id"] == site_id]

    def _group_consoles(self, group: str | None) -> list[str]:
        if not group:
            return []
        g = group.strip().lower()
        return [a.name for a in self.settings.aliases.values() if any(x.lower() == g for x in a.groups)]

    async def _fleet_consoles(self) -> list[str]:
        hosts = await self.resolver.hosts()
        out = []
        for h in hosts:
            state = (h.get("reportedState") or {}).get("state")
            if h.get("owner") and state == "connected":
                out.append(h["id"])
        return out

    async def find(self, macs: list[str] | None = None, ip: str | None = None, name: str | None = None,
                   console: str | None = None, site: str | None = None, group: str | None = None,
                   search_fleet: bool = True) -> dict[str, Any]:
        mac_set = {m for m in (normalize_mac(x) for x in (macs or [])) if m}
        bad = [x for x in (macs or []) if not normalize_mac(x)]
        if not (mac_set or ip or name):
            raise ValueError("give at least one of macs, ip or name")

        plan: list[tuple[str, str | None, str]] = []  # (console query, site query, reason)
        if console:
            plan.append((console, site, "hint"))
        for m in mac_set:
            if m in self.remembered:
                plan.append((self.remembered[m][0], self.remembered[m][1], "remembered"))
        plan += [(c, None, f"group '{group}'") for c in self._group_consoles(group)]

        searched: set[tuple[str, str]] = set()
        errors: list[str] = []

        async def try_console(query: str, site_query: str | None, reason: str) -> list[dict[str, Any]]:
            try:
                target = await self.resolver.console(query)
                sites = await self._sites(target, site_query)
            except (ResolveError, UniFiAPIError) as exc:
                errors.append(f"{query}: {exc}")
                return []
            found = []
            for s in sites:
                key = (target.host_id or target.label, s["id"])
                if key in searched:
                    continue
                searched.add(key)
                try:
                    for hit in await self.search_site(target, s, mac_set, ip, name):
                        found.append({**hit, "found_via": reason})
                except UniFiAPIError as exc:
                    errors.append(f"{target.label}/{s.get('name')}: {exc}")
            return found

        for query, site_query, reason in plan:
            hits = await try_console(query, site_query, reason)
            if hits:
                return self._result(hits, searched, errors, bad)

        if search_fleet:
            consoles = await self._fleet_consoles()
            sem = asyncio.Semaphore(FLEET_CONCURRENCY)

            async def bounded(c: str) -> list[dict[str, Any]]:
                async with sem:
                    return await try_console(c, None, "fleet search")

            results = await asyncio.gather(*(bounded(c) for c in consoles))
            hits = [h for r in results for h in r]
            if hits:
                return self._result(hits, searched, errors, bad)

        return self._result([], searched, errors, bad)

    @staticmethod
    def _result(hits: list[dict[str, Any]], searched: set, errors: list[str], bad: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {"found": bool(hits), "matches": hits, "sites_searched": len(searched)}
        if errors:
            out["errors"] = errors[:20]
        if bad:
            out["ignored_invalid_macs"] = bad
        if not hits:
            out["note"] = ("No connected client matched. UniFi only lists clients that are connected now; "
                           "check the MACs (Windows can use random Wi-Fi MACs) or search_fleet.")
        return out

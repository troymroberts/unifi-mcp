"""UniFi MCP server: Site Manager fleet tools + per-console Network API tools."""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import spec
from .client import HttpBackend, NetworkAPI, NetworkTarget, SiteManagerAPI, UniFiAPIError
from .config import ConfigError, Settings, load_settings
from .resolve import Resolver, ResolveError, host_name

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

SITE_RESOURCES: dict[str, str] = {
    "networks": "networks",
    "wifi": "wifi/broadcasts",
    "firewall-policies": "firewall/policies",
    "firewall-zones": "firewall/zones",
    "acl-rules": "acl-rules",
    "dns-policies": "dns/policies",
    "traffic-matching-lists": "traffic-matching-lists",
    "wans": "wans",
    "vpn-servers": "vpn/servers",
    "vpn-tunnels": "vpn/site-to-site-tunnels",
    "vouchers": "hotspot/vouchers",
    "radius-profiles": "radius/profiles",
    "device-tags": "device-tags",
    "lags": "switching/lags",
    "mc-lag-domains": "switching/mc-lag-domains",
    "switch-stacks": "switching/switch-stacks",
}
SiteResource = Literal[
    "networks", "wifi", "firewall-policies", "firewall-zones", "acl-rules", "dns-policies",
    "traffic-matching-lists", "wans", "vpn-servers", "vpn-tunnels", "vouchers", "radius-profiles",
    "device-tags", "lags", "mc-lag-domains", "switch-stacks",
]
PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

INSTRUCTIONS = """\
UniFi fleet + Network API access.
- Fleet (all consoles, via api.ui.com Site Manager): list_consoles, list_cloud_sites, list_cloud_devices, fleet_health.
- Per console: pass console= as a consoles.yaml alias (e.g. 'home'), a console hostname (partial is fine if
  unique) or a Site Manager host id. Cloud consoles go through the Connector; local aliases go direct.
- site= accepts a site name, internal reference or id; omit it when the console has one site.
- Anything not covered by a curated tool: network_api_endpoints -> network_api_describe -> network_api_request.
- Writes are only available when the server runs with UNIFI_ALLOW_WRITES=true, and every write first returns
  a dry-run preview; repeat the call with confirm=true only after the user agrees.
"""


def _guard(fn):
    """Turn API/resolution failures into tool errors the model can act on."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (UniFiAPIError, ResolveError, ConfigError, KeyError, ValueError) as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            if isinstance(exc, UniFiAPIError) and exc.status == 403:
                message += " (a personal Site Manager key only reaches consoles you own; use an organization key)"
            elif isinstance(exc, UniFiAPIError) and exc.status == 408:
                message += (" (the console did not answer the Connector within 25s; retry later, check its"
                            " Network app/remote access, or add a local_url alias in consoles.yaml)")
            raise ToolError(message) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"network error talking to UniFi: {exc!r}") from exc

    return wrapper


def _site_counts(site: dict[str, Any]) -> dict[str, int]:
    return ((site.get("statistics") or {}).get("counts")) or {}


def summarize_host(host: dict[str, Any], sites: list[dict[str, Any]]) -> dict[str, Any]:
    state = host.get("reportedState") or {}
    mine = [s for s in sites if s.get("hostId") == host["id"]]
    connected = state.get("state") == "connected"
    if not connected:
        connector = "no: console disconnected"
    elif host.get("type") != "console":
        connector = "no: not a UniFi OS console"
    elif not host.get("owner"):
        connector = "only with an organization key (you are not the owner)"
    else:
        connector = "yes"
    return {
        "name": host_name(host),
        "id": host["id"],
        "type": host.get("type"),
        "hardware": (state.get("hardware") or {}).get("shortname"),
        "version": state.get("version"),
        "state": state.get("state"),
        "owner": host.get("owner"),
        "ip": host.get("ipAddress"),
        "sites": [(s.get("meta") or {}).get("desc") for s in mine],
        "devices": sum(_site_counts(s).get("totalDevice", 0) for s in mine),
        "offline_devices": sum(_site_counts(s).get("offlineDevice", 0) for s in mine),
        "connector_access": connector,
    }


def summarize_site(site: dict[str, Any], hosts_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts = _site_counts(site)
    stats = site.get("statistics") or {}
    issues = sum(i.get("count", 0) for i in stats.get("internetIssues") or [] if not i.get("notReported"))
    host = hosts_by_id.get(site.get("hostId"))
    return {
        "name": (site.get("meta") or {}).get("desc"),
        "site_id": site.get("siteId"),
        "console": host_name(host) if host else site.get("hostId"),
        "console_state": ((host or {}).get("reportedState") or {}).get("state"),
        "gateway": (stats.get("gateway") or {}).get("shortname"),
        "devices": counts.get("totalDevice", 0),
        "offline_devices": counts.get("offlineDevice", 0),
        "offline_gateways": counts.get("offlineGatewayDevice", 0),
        "clients": counts.get("wifiClient", 0) + counts.get("wiredClient", 0),
        "guest_clients": counts.get("guestClient", 0),
        "pending_updates": counts.get("pendingUpdateDevice", 0),
        "critical_notifications": counts.get("criticalNotification", 0),
        "internet_issue_intervals": issues,
    }


def summarize_isp(series: dict[str, Any], site_names: dict[str, str], hosts_by_id: dict[str, dict[str, Any]],
                  window: str) -> dict[str, Any]:
    wans = [((p.get("data") or {}).get("wan") or {}, p.get("metricTime")) for p in series.get("periods") or []]
    wans = [(w, t) for w, t in wans if w]
    host = hosts_by_id.get(series.get("hostId"))
    row: dict[str, Any] = {
        "site": site_names.get(series.get("siteId"), series.get("siteId")),
        "console": host_name(host) if host else series.get("hostId"),
        "window": window,
        "periods": len(wans),
    }
    if not wans:
        return row

    def values(key: str) -> list[float]:
        return [w[key] for w, _ in wans if isinstance(w.get(key), (int, float))]

    latency, loss, uptime, downtime = values("avgLatency"), values("packetLoss"), values("uptime"), values("downtime")
    last, last_time = wans[-1]
    row.update({
        "isp": last.get("ispName"),
        "avg_latency_ms": round(sum(latency) / len(latency), 1) if latency else None,
        "max_latency_ms": max(values("maxLatency") or [0]),
        "avg_packet_loss_pct": round(sum(loss) / len(loss), 2) if loss else None,
        "min_uptime_pct": min(uptime) if uptime else None,
        "total_downtime": sum(downtime),
        "latest": {"time": last_time, "download_kbps": last.get("download_kbps"),
                   "upload_kbps": last.get("upload_kbps"), "avg_latency_ms": last.get("avgLatency")},
    })
    return row


DEVICE_FIELDS = ("name", "model", "shortname", "productLine", "ip", "mac", "status", "version",
                 "firmwareStatus", "updateAvailable", "isConsole", "startupTime")


def flatten_devices(groups: list[dict[str, Any]], hosts_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for group in groups:
        host = hosts_by_id.get(group.get("hostId"))
        console = group.get("hostName") or (host_name(host) if host else group.get("hostId"))
        for d in group.get("devices") or []:
            out.append({"console": console, **{k: d.get(k) for k in DEVICE_FIELDS}})
    return out


def create_server(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    settings = settings or load_settings()
    backend = HttpBackend(settings.timeout, settings.rate_limit_per_minute, transport)
    sm = SiteManagerAPI(backend, settings.sm_api_key, settings.cloud_base_url)
    network = NetworkAPI(backend)
    resolver = Resolver(settings, sm, network)
    mcp = MCPServer("unifi", instructions=INSTRUCTIONS, version="0.1.0")

    async def fleet() -> tuple[list[dict], list[dict], dict[str, dict]]:
        hosts = await resolver.hosts()
        sites = await sm.get_all("/v1/sites")
        return hosts, sites, {h["id"]: h for h in hosts}

    async def host_filter(console: str | None) -> dict[str, Any] | None:
        if not console:
            return None
        alias = settings.aliases.get(console.lower())
        if alias and alias.is_local:
            raise ResolveError(f"'{console}' is a local-only alias; fleet tools need a cloud console name")
        return await resolver.find_host(alias.host if alias else console)

    async def target_and_site(console: str | None, site: str | None) -> tuple[NetworkTarget, str]:
        target = await resolver.console(console)
        return target, await resolver.site_id(target, site)

    # ---------------------------------------------------------------- fleet (Site Manager)

    @mcp.tool(annotations=READ)
    @_guard
    async def list_consoles(include_disconnected: bool = False, refresh: bool = False) -> list[dict[str, Any]]:
        """List UniFi consoles/servers on the account with version, state, sites, device counts and
        whether the cloud Connector can reach them. Also shows consoles.yaml aliases."""
        if refresh:
            await resolver.hosts(refresh=True)
        hosts, sites, _ = await fleet()
        rows = [summarize_host(h, sites) for h in hosts]
        if not include_disconnected:
            rows = [r for r in rows if r["state"] == "connected"]
        aliases = {a.name: a for a in settings.aliases.values()}
        for name, alias in aliases.items():
            if alias.is_local:
                rows.append({"name": name, "alias": name, "via": "local", "url": alias.local_url})
            else:
                for r in rows:
                    if alias.host in (r["id"], r["name"]):
                        r["alias"] = name
        return rows

    @mcp.tool(annotations=READ)
    @_guard
    async def list_cloud_sites(console: str | None = None, problems_only: bool = False) -> list[dict[str, Any]]:
        """List every site across the fleet with device/client counts, offline devices and internet issues.
        problems_only keeps sites with offline devices, pending updates, critical notifications or WAN issues."""
        hosts, sites, by_id = await fleet()
        host = await host_filter(console)
        if host:
            sites = [s for s in sites if s.get("hostId") == host["id"]]
        rows = [summarize_site(s, by_id) for s in sites]
        if problems_only:
            rows = [r for r in rows if r["offline_devices"] or r["pending_updates"]
                    or r["critical_notifications"] or r["internet_issue_intervals"]]
        return rows

    @mcp.tool(annotations=READ)
    @_guard
    async def list_cloud_devices(
        console: str | None = None,
        status: str | None = None,
        product_line: Literal["network", "protect", "access", "talk"] | None = None,
        needs_update: bool = False,
    ) -> list[dict[str, Any]]:
        """List devices across the fleet (or one console) from Site Manager. status e.g. 'online'/'offline'."""
        hosts = await resolver.hosts()
        by_id = {h["id"]: h for h in hosts}
        host = await host_filter(console)
        params = {"hostIds[]": [host["id"]]} if host else None
        rows = flatten_devices(await sm.get_all("/v1/devices", params), by_id)
        if status:
            rows = [r for r in rows if (r["status"] or "").lower() == status.lower()]
        if product_line:
            rows = [r for r in rows if r["productLine"] == product_line]
        if needs_update:
            rows = [r for r in rows if r["updateAvailable"] or r["firmwareStatus"] not in (None, "", "upToDate")]
        return rows

    @mcp.tool(annotations=READ)
    @_guard
    async def fleet_health() -> dict[str, Any]:
        """One-call fleet health: disconnected consoles, sites with offline devices or WAN issues,
        devices needing firmware updates, and consoles the Connector cannot reach."""
        hosts, sites, by_id = await fleet()
        consoles = [summarize_host(h, sites) for h in hosts]
        site_rows = [summarize_site(s, by_id) for s in sites]
        devices = flatten_devices(await sm.get_all("/v1/devices"), by_id)
        return {
            "consoles_total": len(consoles),
            "consoles_connected": sum(c["state"] == "connected" for c in consoles),
            "disconnected_consoles": [{k: c[k] for k in ("name", "type", "version", "sites")}
                                      for c in consoles if c["state"] != "connected"],
            "no_connector_access": [c["name"] for c in consoles
                                    if c["state"] == "connected" and c["connector_access"] != "yes"],
            "sites_with_problems": [s for s in site_rows if s["console_state"] == "connected" and (
                s["offline_devices"] or s["internet_issue_intervals"] or s["critical_notifications"])],
            "devices_needing_update": [d for d in devices if d["updateAvailable"]],
        }

    @mcp.tool(annotations=READ)
    @_guard
    async def get_isp_metrics(
        interval: Literal["5m", "1h"] = "1h",
        duration: Literal["24h", "7d", "30d"] | None = None,
        console: str | None = None,
        raw: bool = False,
    ) -> list[dict[str, Any]]:
        """ISP/WAN health per site from Site Manager: ISP, avg/max latency, packet loss, uptime and
        downtime over the window. 5m data supports duration 24h; 1h supports 7d or 30d.
        raw=true returns every period (large: use with console=)."""
        duration = duration or ("24h" if interval == "5m" else "7d")
        result = await sm.get(f"/v1/isp-metrics/{interval}", {"duration": duration})
        series = result.get("data") or []
        host = await host_filter(console)
        if host:
            series = [m for m in series if m.get("hostId") == host["id"]]
        _, sites, by_id = await fleet()
        names = {s.get("siteId"): (s.get("meta") or {}).get("desc") for s in sites}
        if raw:
            return [{"site": names.get(m.get("siteId")), **m} for m in series]
        return [summarize_isp(m, names, by_id, duration) for m in series]

    @mcp.tool(annotations=READ)
    @_guard
    async def cloud_api_get(path: str, query: dict[str, Any] | None = None) -> Any:
        """Read-only GET against any Site Manager path, e.g. /v1/sd-wan-configs or /v1/hosts/{id}."""
        if not path.startswith("/v1/") or path.startswith("/v1/connector"):
            raise ValueError("path must start with /v1/ (use network_api_request for console calls)")
        return await sm.get(path, query)

    # ---------------------------------------------------------------- per console (Network API)

    @mcp.tool(annotations=READ)
    @_guard
    async def list_sites(console: str | None = None) -> dict[str, Any]:
        """List the Network application sites on one console, with their ids."""
        target = await resolver.console(console)
        return {"console": target.label, "via": target.via, "sites": await resolver.sites(target, refresh=True)}

    @mcp.tool(annotations=READ)
    @_guard
    async def list_devices(
        console: str | None = None, site: str | None = None, filter: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """List adopted devices on a console's site (name, model, IP, state, firmware).
        filter uses the Network API filter syntax, e.g. "state.eq('OFFLINE')"."""
        target, site_id = await target_and_site(console, site)
        return await network.get_page(target, f"/v1/sites/{site_id}/devices",
                                      limit=limit, offset=offset, params={"filter": filter})

    @mcp.tool(annotations=READ)
    @_guard
    async def get_device(device_id: str, console: str | None = None, site: str | None = None,
                         include_statistics: bool = True) -> dict[str, Any]:
        """Device details (ports, radios, uplink) plus latest statistics (uptime, CPU, memory, load)."""
        target, site_id = await target_and_site(console, site)
        base = f"/v1/sites/{site_id}/devices/{device_id}"
        result = {"device": await network.request(target, "GET", base)}
        if include_statistics:
            result["statistics"] = await network.request(target, "GET", f"{base}/statistics/latest")
        return result

    @mcp.tool(annotations=READ)
    @_guard
    async def list_pending_devices(console: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Devices waiting for adoption on a console."""
        target = await resolver.console(console)
        return await network.get_page(target, "/v1/pending-devices", limit=limit)

    @mcp.tool(annotations=READ)
    @_guard
    async def list_clients(
        console: str | None = None, site: str | None = None, filter: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """List connected clients on a site. filter uses the Network API filter syntax,
        e.g. "type.eq('WIRELESS')" or "ipAddress.eq('10.1.1.5')"."""
        target, site_id = await target_and_site(console, site)
        return await network.get_page(target, f"/v1/sites/{site_id}/clients",
                                      limit=limit, offset=offset, params={"filter": filter})

    @mcp.tool(annotations=READ)
    @_guard
    async def get_client(client_id: str, console: str | None = None, site: str | None = None) -> Any:
        """Details for one connected client."""
        target, site_id = await target_and_site(console, site)
        return await network.request(target, "GET", f"/v1/sites/{site_id}/clients/{client_id}")

    @mcp.tool(annotations=READ)
    @_guard
    async def list_site_config(
        resource: SiteResource, console: str | None = None, site: str | None = None,
        filter: str | None = None, limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """List a site's configuration objects: networks (VLANs), wifi (SSIDs), firewall-policies,
        firewall-zones, acl-rules, dns-policies, traffic-matching-lists, wans, vpn-servers, vpn-tunnels,
        vouchers, radius-profiles, device-tags, lags, mc-lag-domains, switch-stacks."""
        target, site_id = await target_and_site(console, site)
        return await network.get_page(target, f"/v1/sites/{site_id}/{SITE_RESOURCES[resource]}",
                                      limit=limit, offset=offset, params={"filter": filter})

    # ---------------------------------------------------------------- generic escape hatch

    @mcp.tool(annotations=READ)
    async def network_api_endpoints(search: str | None = None, method: str | None = None) -> list[dict[str, str]]:
        """Search the Network Integration API (v10.6.106) operations by words in path/summary,
        e.g. search='firewall policy'. Use network_api_describe for parameters and body schema."""
        return spec.search(search, method)

    @mcp.tool(annotations=READ)
    @_guard
    async def network_api_describe(method: str, path: str) -> dict[str, Any]:
        """Parameters and request body schema for one Network API operation (path as listed,
        e.g. /v1/sites/{siteId}/networks)."""
        return spec.describe(method, path)

    async def execute_write(target: NetworkTarget, method: str, path: str, body: Any,
                            confirm: bool, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not settings.allow_writes:
            raise ToolError("writes are disabled; restart the server with UNIFI_ALLOW_WRITES=true")
        preview = {"console": target.label, "via": target.via, "method": method, "path": path,
                   "query": params or None, "body": body}
        if not confirm:
            return {"dry_run": True, **preview,
                    "next": "Show this to the user; re-run the same call with confirm=true to execute."}
        result = await network.request(target, method, path, params=params, body=body)
        return {"executed": True, **preview, "result": result}

    @mcp.tool(annotations=WRITE)
    @_guard
    async def network_api_request(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        console: str | None = None,
        site: str | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        confirm: bool = False,
    ) -> Any:
        """Call any Network Integration API path on a console, e.g. GET /v1/sites/{siteId}/networks.
        {siteId} is filled from site= (or the console's only site). GETs run immediately; other methods
        need UNIFI_ALLOW_WRITES=true and return a dry-run until confirm=true."""
        if not path.startswith("/v1/"):
            raise ValueError("path must start with /v1/ (Network API path, without the /integration prefix)")
        target = await resolver.console(console)
        if "{siteId}" in path:
            path = path.replace("{siteId}", await resolver.site_id(target, site))
        if leftover := PATH_PARAM_RE.findall(path):
            raise ValueError(f"fill in path parameters: {', '.join(leftover)}")
        if method == "GET":
            return await network.request(target, "GET", path, params=query)
        return await execute_write(target, method, path, body, confirm, query)

    # ---------------------------------------------------------------- curated writes

    if settings.allow_writes:

        @mcp.tool(annotations=WRITE)
        @_guard
        async def restart_device(device_id: str, console: str | None = None, site: str | None = None,
                                 confirm: bool = False) -> dict[str, Any]:
            """Restart an adopted device (AP, switch, gateway). Dry-run unless confirm=true."""
            target, site_id = await target_and_site(console, site)
            return await execute_write(target, "POST", f"/v1/sites/{site_id}/devices/{device_id}/actions",
                                       {"action": "RESTART"}, confirm)

        @mcp.tool(annotations=WRITE)
        @_guard
        async def power_cycle_port(device_id: str, port_idx: int, console: str | None = None,
                                   site: str | None = None, confirm: bool = False) -> dict[str, Any]:
            """PoE power-cycle one switch port (reboots whatever is powered from it). Dry-run unless confirm=true."""
            target, site_id = await target_and_site(console, site)
            path = f"/v1/sites/{site_id}/devices/{device_id}/interfaces/ports/{port_idx}/actions"
            return await execute_write(target, "POST", path, {"action": "POWER_CYCLE"}, confirm)

        @mcp.tool(annotations=WRITE)
        @_guard
        async def set_guest_access(
            client_id: str, authorize: bool = True, console: str | None = None, site: str | None = None,
            time_limit_minutes: int | None = None, data_usage_limit_mbytes: int | None = None,
            rx_rate_limit_kbps: int | None = None, tx_rate_limit_kbps: int | None = None,
            confirm: bool = False,
        ) -> dict[str, Any]:
            """Authorize (or revoke) guest network access for a connected client. Dry-run unless confirm=true."""
            target, site_id = await target_and_site(console, site)
            body: dict[str, Any] = {"action": "AUTHORIZE_GUEST_ACCESS" if authorize else "UNAUTHORIZE_GUEST_ACCESS"}
            if authorize:
                limits = {"timeLimitMinutes": time_limit_minutes, "dataUsageLimitMBytes": data_usage_limit_mbytes,
                          "rxRateLimitKbps": rx_rate_limit_kbps, "txRateLimitKbps": tx_rate_limit_kbps}
                body.update({k: v for k, v in limits.items() if v is not None})
            return await execute_write(target, "POST", f"/v1/sites/{site_id}/clients/{client_id}/actions",
                                       body, confirm)

        @mcp.tool(annotations=WRITE)
        @_guard
        async def create_vouchers(
            name: str, time_limit_minutes: int, count: int = 1, console: str | None = None,
            site: str | None = None, authorized_guest_limit: int | None = None,
            data_usage_limit_mbytes: int | None = None, rx_rate_limit_kbps: int | None = None,
            tx_rate_limit_kbps: int | None = None, confirm: bool = False,
        ) -> dict[str, Any]:
            """Generate hotspot guest vouchers. Dry-run unless confirm=true."""
            target, site_id = await target_and_site(console, site)
            body = {"name": name, "count": count, "timeLimitMinutes": time_limit_minutes,
                    "authorizedGuestLimit": authorized_guest_limit, "dataUsageLimitMBytes": data_usage_limit_mbytes,
                    "rxRateLimitKbps": rx_rate_limit_kbps, "txRateLimitKbps": tx_rate_limit_kbps}
            body = {k: v for k, v in body.items() if v is not None}
            return await execute_write(target, "POST", f"/v1/sites/{site_id}/hotspot/vouchers", body, confirm)

    return mcp


def main() -> None:
    # httpx logs every request URL at INFO; keep stderr quiet unless something is wrong.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server().run()

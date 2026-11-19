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
import time
from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget, UniFiAPIError
from .config import Settings
from .resolve import ResolveError, Resolver

MAC_RE = re.compile(r"^[0-9a-f]{12}$")
FLEET_CONCURRENCY = 4


def band_ghz(radio: Any, channel: Any = None) -> float | None:
    """Wi-Fi band in GHz (2.4, 5 or 6) from the classic ``radio`` field, falling back to the channel.

    UniFi ``stat/sta`` reports ``radio`` as ``ng`` (2.4 GHz), ``na`` (5 GHz) or ``6e`` (6 GHz).
    """
    r = str(radio or "").lower()
    if r in ("ng", "2g", "24g"):
        return 2.4
    if r in ("na", "5g"):
        return 5.0
    if "6e" in r or r in ("6g", "ax6", "6ghz"):
        return 6.0
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if 1 <= ch <= 14:
        return 2.4
    if 32 <= ch <= 177:
        return 5.0
    return None


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None (keeps tool output compact)."""
    return {k: v for k, v in d.items() if v is not None}


def _now_ms() -> int:
    return int(time.time() * 1000)


def event_kind(key: Any) -> str | None:
    """Map a classic ``stat/event`` key to a diagnostic category, or None to ignore it."""
    k = str(key or "")
    if "Roam" in k:
        return "roam"
    if "PortDown" in k:
        return "port_down"
    if "PortUp" in k:
        return "port_up"
    if "Lost_Contact" in k or "LostContact" in k:
        return "lost_contact"
    if "Restarted" in k or "Reboot" in k or "Upgrade" in k:
        return "restart"
    if "STP" in k or "Blocking" in k:
        return "stp"
    if "Disconnect" in k:
        return "disconnect"
    if "Connected" in k or "Connect" in k:
        return "connect"
    return None


def _num(v: Any) -> float | None:
    """Parse a possibly-string number (UniFi returns cpu/mem as strings); None if not numeric."""
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _sat(v: Any) -> float | None:
    """UniFi satisfaction/experience score; -1 (and other negatives) mean 'not computed' -> None."""
    n = _num(v)
    return None if (n is None or n < 0) else n


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
            "radio": r.get("radio") if not wired else None,
            "radio_proto": r.get("radio_proto") if not wired else None,
            "channel": r.get("channel") if not wired else None,
            "band_ghz": band_ghz(r.get("radio"), r.get("channel")) if not wired else None,
            "signal_dbm": (r.get("signal") if r.get("signal") is not None else r.get("rssi")) if not wired else None,
            "satisfaction": _sat(r.get("satisfaction")) if not wired else None,
            "tx_rate_kbps": r.get("tx_rate") if not wired else None,
            "rx_rate_kbps": r.get("rx_rate") if not wired else None,
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
                entry["access_point"] = _prune({
                    "name": ap.get("name"), "model": ap.get("model"), "mac": c["ap_mac"], "ssid": c.get("ssid"),
                    "band_ghz": c.get("band_ghz"), "channel": c.get("channel"), "signal_dbm": c.get("signal_dbm"),
                    "satisfaction": c.get("satisfaction"),
                })
            if c.get("uplink_device_id"):
                up = devices.get(c["uplink_device_id"], {})
                key = "switch" if c.get("connection") == "wired" else "access_point"
                entry[key] = {"name": up.get("name"), "model": up.get("model"), "mac": up.get("mac"),
                              "port": None, "note": "Integration API fallback: no switch port available"}
            results.append(entry)
            if c.get("mac"):
                self.remembered[c["mac"]] = (target.host_id or target.label, site.get("internalReference"))
        return results

    async def list_wireless(self, console: str | None, site: str | None, band: float | None = None,
                            network: str | None = None, ssid: str | None = None) -> dict[str, Any]:
        """Wireless clients on a console/site with band/channel/signal/AP/SSID, from classic stat/sta.

        Optional filters: ``band`` (2.4, 5 or 6 GHz), ``network`` (substring of the network/VLAN name)
        and ``ssid`` (exact). If a console only answers the Integration API, band/signal are unavailable.
        """
        target = await self.resolver.console(console)
        sites = await self._sites(target, site)
        want_band = None if band is None else float(band)
        net = (network or "").strip().lower()
        want_ssid = (ssid or "").strip().lower()
        clients: list[dict[str, Any]] = []
        source: str | None = None
        for s in sites:
            rows, source = await self._site_clients(target, s)
            devices = await self._site_devices(target, s)
            for c in rows:
                if c.get("connection") != "wireless":
                    continue
                if net and net not in str(c.get("network") or "").lower():
                    continue
                if want_ssid and want_ssid != str(c.get("ssid") or "").lower():
                    continue
                if want_band is not None and c.get("band_ghz") != want_band:
                    continue
                entry = _prune({
                    "mac": c.get("mac"), "ip": c.get("ip"), "name": c.get("name"),
                    "network": c.get("network"), "vlan": c.get("vlan"),
                    "band_ghz": c.get("band_ghz"), "channel": c.get("channel"), "ssid": c.get("ssid"),
                    "signal_dbm": c.get("signal_dbm"), "satisfaction": c.get("satisfaction"),
                    "tx_rate_kbps": c.get("tx_rate_kbps"), "rx_rate_kbps": c.get("rx_rate_kbps"),
                    "site": s.get("name"),
                })
                if c.get("ap_mac"):
                    ap = devices.get(c["ap_mac"], {})
                    entry["access_point"] = _prune({"name": ap.get("name"), "model": ap.get("model"),
                                                    "mac": c["ap_mac"]})
                clients.append(entry)
        clients.sort(key=lambda x: (x.get("band_ghz") or 0, str(x.get("ip") or "")))
        out: dict[str, Any] = {"count": len(clients), "clients": clients}
        if source:
            out["source"] = source
        if source and source != "legacy stat/sta":
            out["note"] = ("band/signal come from the classic stat/sta API; this console returned the "
                           "Integration API, so band, channel and signal are unavailable here.")
        return out

    async def list_events(self, console: str | None, site: str | None, macs: list[str] | None = None,
                          kinds: list[str] | None = None, hours: int = 24, limit: int = 200) -> dict[str, Any]:
        """Recent wireless client events (roam / connect / disconnect) from the classic ``stat/event`` feed.

        Shows roaming handoffs (from-AP -> to-AP, channel/radio change), connects and disconnects, with
        AP names resolved. Filter by client ``macs``, ``kinds`` (roam/connect/disconnect) and ``hours``.
        The Integration API has no event history, so a console that only answers it returns nothing here.
        """
        target = await self.resolver.console(console)
        sites = await self._sites(target, site)
        mac_set = {m for m in (normalize_mac(x) for x in (macs or [])) if m}
        want_kinds = {k.lower() for k in (kinds or [])}
        cutoff_ms = (_now_ms() - hours * 3_600_000) if hours else None
        events: list[dict[str, Any]] = []
        reachable = False
        for s in sites:
            try:
                rows = await self._legacy(target, s["internalReference"], "/stat/event")
            except UniFiAPIError:
                continue
            reachable = True
            devices = await self._site_devices(target, s)

            def ap_name(mac: Any) -> str | None:
                nm = normalize_mac(mac)
                return (devices.get(nm, {}).get("name") or nm) if nm else None

            for e in rows:
                key = str(e.get("key") or "")
                kind = event_kind(key)
                if kind is None or (want_kinds and kind not in want_kinds):
                    continue
                umac = normalize_mac(e.get("user") or e.get("client") or e.get("guest"))
                if mac_set and umac not in mac_set:
                    continue
                t_ms = e.get("time") or e.get("timestamp")
                if cutoff_ms and isinstance(t_ms, (int, float)) and t_ms < cutoff_ms:
                    continue
                events.append(_prune({
                    "time": e.get("datetime"), "time_ms": t_ms, "kind": kind, "key": key, "client": umac,
                    "hostname": e.get("hostname"), "ssid": e.get("ssid"),
                    "device": ap_name(e.get("sw") or e.get("ap") or e.get("gw")),
                    "port": e.get("port") or e.get("sw_port"),
                    "ap": ap_name(e.get("ap")), "from_ap": ap_name(e.get("ap_from")),
                    "to_ap": ap_name(e.get("ap_to")), "channel_from": e.get("channel_from"),
                    "channel_to": e.get("channel_to"), "radio_from": e.get("radio_from"),
                    "radio_to": e.get("radio_to"), "duration_s": e.get("duration"), "msg": e.get("msg"),
                }))
        events.sort(key=lambda x: x.get("time_ms") or 0, reverse=True)
        out: dict[str, Any] = {"count": len(events), "events": events[:limit]}
        if not reachable:
            out["note"] = ("No classic stat/event data: this console only answered the Integration API, "
                           "which has no event history.")
        return out

    @staticmethod
    def _device_entry(d: dict[str, Any], site_name: str | None) -> dict[str, Any]:
        sysstats = d.get("system-stats") or {}
        sys2 = d.get("sys_stats") or {}
        state = d.get("state")
        cpu, mem, temp = _num(sysstats.get("cpu")), _num(sysstats.get("mem")), _num(d.get("general_temperature"))
        problems: list[str] = []
        if state is not None and state != 1:
            problems.append(f"not connected (state={state})")
        if cpu is not None and cpu >= 85:
            problems.append(f"cpu {cpu}%")
        if mem is not None and mem >= 90:
            problems.append(f"mem {mem}%")
        if d.get("overheating"):
            problems.append("overheating")
        if d.get("upgradable"):
            problems.append("firmware update available")
        entry: dict[str, Any] = {
            "name": d.get("name"), "model": d.get("model"), "type": d.get("type"),
            "mac": normalize_mac(d.get("mac")), "ip": d.get("ip"), "state": state,
            "uptime_s": d.get("uptime") or sys2.get("uptime"), "version": d.get("version"),
            "cpu_pct": cpu, "mem_pct": mem, "temp_c": temp, "loadavg_1": _num(sys2.get("loadavg_1")),
            "site": site_name,
        }
        bad_ports = []
        for p in d.get("port_table") or []:
            issues = []
            if (p.get("rx_errors") or 0) or (p.get("tx_errors") or 0):
                issues.append(f"errors rx{p.get('rx_errors', 0)}/tx{p.get('tx_errors', 0)}")
            # NOTE: rx_dropped/tx_dropped are NOT flagged — UniFi counts normal flooded
            # broadcast/multicast frames there, so nearly every access port would trip.
            if str(p.get("stp_state") or "").lower() == "blocking":
                issues.append("stp-blocking")
            if p.get("poe_enable") and p.get("poe_good") is False:
                issues.append("poe-fault")
            if issues:
                bad_ports.append(_prune({"port": p.get("port_idx"), "name": p.get("name"), "up": p.get("up"),
                                         "speed": p.get("speed"), "stp_state": p.get("stp_state"),
                                         "rx_errors": p.get("rx_errors"), "tx_errors": p.get("tx_errors"),
                                         "issues": issues}))
        if bad_ports:
            entry["problem_ports"] = bad_ports
            problems.append(f"{len(bad_ports)} port issue(s)")
        radios = []
        for r in d.get("radio_table_stats") or []:
            airtime = _num(r.get("cu_total"))
            self_use = (_num(r.get("cu_self_rx")) or 0) + (_num(r.get("cu_self_tx")) or 0)
            ext = None if airtime is None else round(max(0.0, airtime - self_use), 1)
            b = band_ghz(r.get("radio"), r.get("channel"))
            radios.append(_prune({
                "band_ghz": b, "channel": r.get("channel"), "clients": r.get("num_sta"),
                "airtime_pct": airtime, "ext_interference_pct": ext,
                "tx_retries_pct": _num(r.get("tx_retries_pct") if r.get("tx_retries_pct") is not None
                                       else r.get("tx_retries")),
                "satisfaction": _sat(r.get("satisfaction")), "tx_power": r.get("tx_power"),
            }))
            if airtime is not None and airtime >= 80:
                problems.append(f"{b} GHz airtime {airtime}%")
        if radios:
            entry["radios"] = radios
        if problems:
            entry["problems"] = problems
        return _prune(entry)

    async def device_health(self, console: str | None, site: str | None,
                            problems_only: bool = False) -> dict[str, Any]:
        """Per-device health from classic ``stat/device``: CPU/RAM/temp/uptime, switch port errors/STP-blocking/
        PoE faults, and AP per-radio airtime/external-interference/TX-retries/client-count. ``problems_only``
        returns just the devices with a flagged issue. Needs a console that answers the classic API."""
        target = await self.resolver.console(console)
        sites = await self._sites(target, site)
        devices: list[dict[str, Any]] = []
        source: str | None = None
        for s in sites:
            try:
                rows = await self._legacy(target, s["internalReference"], "/stat/device")
            except UniFiAPIError:
                continue
            source = "legacy stat/device"
            devices.extend(self._device_entry(d, s.get("name")) for d in rows)
        if problems_only:
            devices = [d for d in devices if d.get("problems")]
        out: dict[str, Any] = {"count": len(devices), "devices": devices}
        if source:
            out["source"] = source
        else:
            out["note"] = "No classic stat/device data: this console only answered the Integration API."
        return out

    async def site_health(self, console: str | None, site: str | None) -> dict[str, Any]:
        """Per-subsystem site health from classic ``stat/health``: WAN/WWW latency and packet drops,
        throughput, and device/client counts — the 'Site Health' row of the UniFi dashboard."""
        target = await self.resolver.console(console)
        sites = await self._sites(target, site)
        out_sites = []
        for s in sites:
            try:
                rows = await self._legacy(target, s["internalReference"], "/stat/health")
            except UniFiAPIError:
                continue
            subs = [_prune({
                "subsystem": h.get("subsystem"), "status": h.get("status"), "latency_ms": h.get("latency"),
                "drops": h.get("drops"), "xput_up_mbps": h.get("xput_up"), "xput_down_mbps": h.get("xput_down"),
                "speedtest_ping_ms": h.get("speedtest_ping"), "wan_ip": h.get("wan_ip"),
                "num_user": h.get("num_user"), "num_ap": h.get("num_ap"), "num_sw": h.get("num_sw"),
                "num_disconnected": h.get("num_disconnected"),
            }) for h in rows]
            out_sites.append({"site": s.get("name"), "subsystems": subs})
        return {"sites": out_sites}

    async def client_app_usage(self, console: str | None, site: str | None, macs: list[str] | None = None,
                               top: int = 12, min_mb: float = 0.0) -> dict[str, Any]:
        """Per-client application traffic from classic DPI (``stat/stadpi``), tied to the client device.

        Returns each client's total data and its top apps and categories by bytes (DPI measures *data
        volume*, not time-on-app). App/category are the DPI ids plus any name the controller supplies.
        Filter by ``macs``; ``min_mb`` drops tiny talkers. Needs DPI enabled on the site and a console that
        answers the classic API.
        """
        target = await self.resolver.console(console)
        sites = await self._sites(target, site)
        mac_set = {m for m in (normalize_mac(x) for x in (macs or [])) if m}
        clients: list[dict[str, Any]] = []
        reachable = False
        for s in sites:
            try:
                rows = await self._legacy(target, s["internalReference"], "/stat/stadpi")
            except UniFiAPIError:
                continue
            reachable = True
            try:
                names = {c["mac"]: c for c in (await self._site_clients(target, s))[0] if c.get("mac")}
            except UniFiAPIError:
                names = {}

            def bytes_of(rec: dict[str, Any]) -> int:
                return int(rec.get("tx_bytes") or 0) + int(rec.get("rx_bytes") or 0)

            def label(rec: dict[str, Any], id_key: str) -> dict[str, Any]:
                b = bytes_of(rec)
                out = {id_key: rec.get(id_key), "bytes": b, "mb": round(b / 1_000_000, 1)}
                for nk in ("application", "app_name", "category", "cat_name", "name"):
                    if rec.get(nk):
                        out["name"] = rec[nk]
                        break
                return _prune(out)

            for st in rows:
                mac = normalize_mac(st.get("mac"))
                if mac_set and mac not in mac_set:
                    continue
                by_app = sorted((st.get("by_app") or []), key=bytes_of, reverse=True)
                by_cat = sorted((st.get("by_cat") or []), key=bytes_of, reverse=True)
                total = sum(bytes_of(a) for a in by_app) or sum(bytes_of(c) for c in by_cat)
                if total / 1_000_000 < min_mb:
                    continue
                info = names.get(mac, {})
                clients.append(_prune({
                    "mac": mac, "name": info.get("name"), "ip": info.get("ip"),
                    "total_mb": round(total / 1_000_000, 1),
                    "apps": [label(a, "app") for a in by_app[:top]],
                    "categories": [label(c, "cat") for c in by_cat[:top]],
                }))
        clients.sort(key=lambda x: x.get("total_mb") or 0, reverse=True)
        out: dict[str, Any] = {"count": len(clients), "clients": clients,
                               "note": "DPI measures data volume (bytes) per app, not time spent."}
        if not reachable:
            out["note"] = ("No classic stat/stadpi data: this console only answered the Integration API, "
                           "which has no DPI.")
        elif not clients:
            out["note"] = ("DPI returned no data — enable Deep Packet Inspection and DPI stats on the site "
                           "(UniFi Network > Settings), then retry.")
        return out

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

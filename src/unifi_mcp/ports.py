"""Switch port settings (name, native VLAN, tagged VLANs, port profile, PoE) through the classic API.

The official Network Integration API can only power-cycle a port. Port configuration lives in the
classic ``rest/device`` record as ``port_overrides`` (one entry per customised port), which the
api.ui.com Connector proxies with the same Site Manager key (as ``find_client`` does).

Reads come from ``stat/device`` (port_table + port_overrides), ``stat/sta`` (which client MACs are
learned on which port), ``rest/networkconf`` (networks/VLANs) and ``rest/portconf`` (port profiles).
A write PUTs the device's full ``port_overrides`` list with only the requested ports changed.
"""

from __future__ import annotations

from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget
from .config import Settings
from .locate import normalize_mac
from .resolve import Resolver

MAX_PORTS = 64
POE_MODES = {"auto", "off"}
TAGGED_MODES = {"all": "auto", "none": "block_all"}


class PortError(ValueError):
    pass


class SwitchPorts:
    def __init__(self, settings: Settings, resolver: Resolver, network: NetworkAPI):
        self.settings = settings
        self.resolver = resolver
        self.network = network

    # ------------------------------------------------------------------ classic API helpers

    async def _site_ref(self, target: NetworkTarget, site: str | None) -> tuple[str, str]:
        site_id = await self.resolver.site_id(target, site)
        for s in await self.resolver.sites(target):
            if s["id"] == site_id:
                return s["internalReference"], s.get("name") or s["internalReference"]
        raise PortError(f"site {site!r} not found")

    async def _legacy(self, target: NetworkTarget, site_ref: str, method: str, path: str, body: Any = None) -> list[dict[str, Any]]:
        res = await self.network.request(target, method, f"/s/{site_ref}{path}", body=body, app=NETWORK_LEGACY_APP)
        return res.get("data", []) if isinstance(res, dict) else []

    async def _context(self, target: NetworkTarget, site_ref: str) -> dict[str, Any]:
        devices = await self._legacy(target, site_ref, "GET", "/stat/device")
        networks = [n for n in await self._legacy(target, site_ref, "GET", "/rest/networkconf")
                    if n.get("purpose") not in ("wan", "remote-user-vpn", "site-vpn", "vpn-client")]
        try:
            profiles = await self._legacy(target, site_ref, "GET", "/rest/portconf")
        except Exception:
            profiles = []
        try:
            clients = await self._legacy(target, site_ref, "GET", "/stat/sta")
        except Exception:
            clients = []
        return {"devices": devices, "networks": networks, "profiles": profiles, "clients": clients}

    @staticmethod
    def _net_label(n: dict[str, Any] | None) -> str | None:
        if not n:
            return None
        return f"{n.get('name')} (vlan {n.get('vlan') or 1})"

    @staticmethod
    def _find_device(devices: list[dict[str, Any]], query: str) -> dict[str, Any]:
        q = str(query).strip()
        mac = normalize_mac(q)
        hits = [d for d in devices if d.get("_id") == q or (mac and normalize_mac(d.get("mac")) == mac)]
        if not hits:
            hits = [d for d in devices if str(d.get("name") or "").lower() == q.lower()]
        if not hits:
            hits = [d for d in devices if q.lower() in str(d.get("name") or "").lower()]
        switches = [d for d in hits if d.get("port_table")]
        if len(switches) == 1:
            return switches[0]
        if not switches:
            raise PortError(f"no switch matches {query!r}" + (f" (matched non-switch {hits[0].get('name')!r})" if hits else ""))
        raise PortError(f"{query!r} matches several switches: " + ", ".join(repr(d.get("name")) for d in switches))

    @staticmethod
    def _pick(items: list[dict[str, Any]], query: Any, kind: str, vlan_ok: bool = False) -> dict[str, Any]:
        q = str(query).strip().lower()
        for it in items:
            keys = {str(it.get("_id", "")).lower(), str(it.get("name") or "").strip().lower()}
            if vlan_ok:
                keys.add(str(it.get("vlan") or "1"))
            if q in keys:
                return it
        names = ", ".join(repr(it.get("name")) + (f" (vlan {it.get('vlan') or 1})" if vlan_ok else "") for it in items)
        raise PortError(f"{kind} {query!r} not found; {kind}s: {names}")

    def _port_view(self, dev: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
        nets = {n.get("_id"): n for n in ctx["networks"]}
        profiles = {p.get("_id"): p for p in ctx["profiles"]}
        overrides = {o.get("port_idx"): o for o in dev.get("port_overrides") or []}
        dev_mac = normalize_mac(dev.get("mac"))
        uplink_idx = (dev.get("uplink") or {}).get("port_idx")
        downstream: dict[int, list[str]] = {}
        for d in ctx["devices"]:
            up = d.get("uplink") or {}
            if normalize_mac(up.get("uplink_mac")) == dev_mac and up.get("uplink_remote_port") is not None:
                downstream.setdefault(int(up["uplink_remote_port"]), []).append(d.get("name") or d.get("mac"))
        by_port: dict[int, list[dict[str, Any]]] = {}
        for c in ctx["clients"]:
            if normalize_mac(c.get("sw_mac")) == dev_mac and c.get("sw_port") is not None:
                by_port.setdefault(int(c["sw_port"]), []).append(
                    {"mac": normalize_mac(c.get("mac")), "name": c.get("name") or c.get("hostname"), "ip": c.get("ip")})
        rows = []
        for p in sorted(dev.get("port_table") or [], key=lambda p: p.get("port_idx", 0)):
            idx = p.get("port_idx")
            o = overrides.get(idx, {})
            merged = {**p, **o}
            excluded = [nets.get(x, {}).get("name", x) for x in merged.get("excluded_networkconf_ids") or []]
            tagged = {"auto": "all", "block_all": "none", "custom": "custom"}.get(merged.get("tagged_vlan_mgmt"), merged.get("tagged_vlan_mgmt"))
            rows.append({
                "port": idx, "name": merged.get("name"), "up": bool(p.get("up")), "speed_mbps": p.get("speed") if p.get("up") else None,
                "profile": (profiles.get(merged.get("portconf_id")) or {}).get("name") if merged.get("portconf_id") else None,
                "setting": merged.get("setting_preference"),
                "native_network": self._net_label(nets.get(merged.get("native_networkconf_id"))),
                "tagged": tagged, "tagged_excluded": excluded or None,
                "poe_mode": merged.get("poe_mode"), "poe_watts": p.get("poe_power"),
                "uplink": bool(p.get("is_uplink")) or idx == uplink_idx,
                "lag": bool(p.get("aggregated_by") or p.get("lag_member") or merged.get("op_mode") == "aggregate"),
                "downstream_devices": downstream.get(idx), "clients": by_port.get(idx, []),
                "overridden": idx in overrides,
            })
        return rows

    # ------------------------------------------------------------------ read

    async def list_ports(self, device: str, console: str | None, site: str | None, only_up: bool = False) -> dict[str, Any]:
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        ctx = await self._context(target, site_ref)
        dev = self._find_device(ctx["devices"], device)
        ports = self._port_view(dev, ctx)
        if only_up:
            ports = [p for p in ports if p["up"]]
        return {"console": target.label, "site": site_name, "device": dev.get("name"), "model": dev.get("model"),
                "mac": normalize_mac(dev.get("mac")), "ports": ports,
                "note": "clients = MACs learned on the port right now (classic stat/sta); several MACs on an "
                        "access port usually mean an unmanaged switch or AP behind it."}

    # ------------------------------------------------------------------ write (plan, then apply)

    async def set_ports(self, device: str, ports: list[dict[str, Any]], console: str | None, site: str | None,
                        confirm: bool) -> dict[str, Any]:
        if not ports:
            raise PortError("give at least one port {port, name?, native_network?, tagged?, profile?, poe_mode?}")
        if len(ports) > MAX_PORTS:
            raise PortError(f"at most {MAX_PORTS} ports per call")
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        ctx = await self._context(target, site_ref)
        dev = self._find_device(ctx["devices"], device)
        view = {p["port"]: p for p in self._port_view(dev, ctx)}
        overrides = [dict(o) for o in dev.get("port_overrides") or []]
        by_idx = {o.get("port_idx"): o for o in overrides}

        plan, errors, seen = [], [], set()
        for i, item in enumerate(ports):
            idx = item.get("port")
            label = f"item {i + 1} (port {idx})"
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                errors.append(f"{label}: port must be a port number"); continue
            if idx not in view:
                errors.append(f"{label}: {dev.get('name')} has no port {idx}"); continue
            if idx in seen:
                errors.append(f"{label}: port listed twice"); continue
            seen.add(idx)
            cur = view[idx]
            wants = {k for k in ("name", "native_network", "tagged", "profile", "poe_mode") if item.get(k) is not None}
            if not wants:
                errors.append(f"{label}: nothing to change"); continue
            vlan_change = bool(wants & {"native_network", "tagged", "profile"})
            if cur["uplink"] and (vlan_change or "poe_mode" in wants):
                errors.append(f"{label}: is {dev.get('name')}'s uplink; refusing VLAN/profile/PoE changes (would cut the switch off)"); continue
            if cur["lag"] and vlan_change:
                errors.append(f"{label}: is part of a link aggregation; change the LAG, not one member"); continue
            if cur["downstream_devices"] and (vlan_change or item.get("poe_mode") == "off"):
                errors.append(f"{label}: feeds UniFi device(s) {cur['downstream_devices']}; refusing VLAN/profile/PoE-off changes"); continue
            if "profile" in wants and wants & {"native_network", "tagged"}:
                errors.append(f"{label}: give either profile or native_network/tagged, not both"); continue

            change: dict[str, Any] = {}
            try:
                if "name" in wants:
                    change["name"] = str(item["name"])[:64]
                if "profile" in wants:
                    prof = self._pick(ctx["profiles"], item["profile"], "profile")
                    change.update({"portconf_id": prof["_id"], "setting_preference": "auto"})
                if "native_network" in wants:
                    net = self._pick(ctx["networks"], item["native_network"], "network", vlan_ok=True)
                    change.update({"native_networkconf_id": net["_id"], "setting_preference": "manual"})
                if "tagged" in wants:
                    t = item["tagged"]
                    if isinstance(t, str) and t.lower() in TAGGED_MODES:
                        change.update({"tagged_vlan_mgmt": TAGGED_MODES[t.lower()], "excluded_networkconf_ids": [],
                                       "setting_preference": "manual"})
                    elif isinstance(t, list):
                        keep = {self._pick(ctx["networks"], x, "network", vlan_ok=True)["_id"] for x in t}
                        native_id = change.get("native_networkconf_id") or by_idx.get(idx, {}).get("native_networkconf_id")
                        excluded = [n["_id"] for n in ctx["networks"] if n["_id"] not in keep and n["_id"] != native_id]
                        change.update({"tagged_vlan_mgmt": "custom", "excluded_networkconf_ids": excluded,
                                       "setting_preference": "manual"})
                    else:
                        raise PortError(f"tagged must be 'all', 'none' or a list of networks, not {t!r}")
                if "poe_mode" in wants:
                    mode = str(item["poe_mode"]).lower()
                    if mode not in POE_MODES:
                        raise PortError(f"poe_mode must be one of {sorted(POE_MODES)}")
                    change["poe_mode"] = mode
            except PortError as exc:
                errors.append(f"{label}: {exc}"); continue

            existing = by_idx.get(idx, {})
            if all(existing.get(k) == v for k, v in change.items()):
                plan.append({"port": idx, "action": "none", "_change": {}})
                continue
            nets = {n["_id"]: n for n in ctx["networks"]}
            after = {**cur}
            if "name" in change:
                after["name"] = change["name"]
            if "native_networkconf_id" in change:
                after["native_network"] = self._net_label(nets.get(change["native_networkconf_id"]))
            if "tagged_vlan_mgmt" in change:
                after["tagged"] = {"auto": "all", "block_all": "none", "custom": "custom"}[change["tagged_vlan_mgmt"]]
                after["tagged_excluded"] = [nets[x]["name"] for x in change["excluded_networkconf_ids"]] or None
            if "portconf_id" in change:
                after["profile"] = self._pick(ctx["profiles"], change["portconf_id"], "profile").get("name")
            if "poe_mode" in change:
                after["poe_mode"] = change["poe_mode"]
            keys = ("name", "profile", "native_network", "tagged", "tagged_excluded", "poe_mode")
            plan.append({"port": idx, "action": "update" if idx in by_idx else "create override",
                         "before": {k: cur[k] for k in keys}, "after": {k: after[k] for k in keys},
                         "clients_affected": cur["clients"], "_change": change})

        changes = [p for p in plan if p["action"] != "none"]
        summary = {"console": target.label, "site": site_name, "device": dev.get("name"),
                   "ports": len(ports), "changes": len(changes), "unchanged": len(plan) - len(changes), "errors": errors}
        public = [{k: v for k, v in p.items() if not k.startswith("_")} for p in changes]
        if errors:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Fix the errors; nothing is written while any port has an error."}
        if not changes:
            return {"dry_run": not confirm, **summary, "plan": [], "next": "Nothing to change."}
        if not self.settings.allow_writes:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Writes are disabled; restart the server with UNIFI_ALLOW_WRITES=true to apply."}
        if not confirm:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Show this plan to the user (note clients_affected: they move with a VLAN change); "
                            "re-run the same call with confirm=true to apply it."}

        for p in changes:
            o = by_idx.get(p["port"])
            if o is None:
                o = {"port_idx": p["port"]}
                overrides.append(o)
                by_idx[p["port"]] = o
            if p["_change"].get("setting_preference") == "auto":
                for k in ("native_networkconf_id", "tagged_vlan_mgmt", "excluded_networkconf_ids"):
                    o.pop(k, None)
            o.update(p["_change"])
        await self._legacy(target, site_ref, "PUT", f"/rest/device/{dev['_id']}", {"port_overrides": overrides})
        return {"executed": True, **summary, "applied": [{"port": p["port"], **p["after"]} for p in changes],
                "note": "The switch re-provisions (a few seconds). Clients on a port whose VLAN changed get a new "
                        "address on their next DHCP request; power-cycle the port or device to force it. "
                        "Check with list_switch_ports."}

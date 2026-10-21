"""DHCP reservations (fixed IPs) and client names, through the classic controller API.

The official Network Integration API has no way to read or set a client's fixed IP or alias, so
this uses the classic ``rest/user`` (known clients) and ``rest/networkconf`` (networks) routes, which
the api.ui.com Connector proxies with the same Site Manager key (as ``find_client`` does).

A fixed IP may sit outside the network's DHCP range (verified on UniFi OS Server 5.1).
"""

from __future__ import annotations

import ipaddress
from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget
from .config import Settings
from .locate import normalize_mac
from .resolve import Resolver

MAX_BATCH = 300


class ReservationError(ValueError):
    pass


class Reservations:
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
        raise ReservationError(f"site {site!r} not found")

    async def _legacy(self, target: NetworkTarget, site_ref: str, method: str, path: str, body: Any = None) -> list[dict[str, Any]]:
        res = await self.network.request(target, method, f"/s/{site_ref}{path}", body=body, app=NETWORK_LEGACY_APP)
        return res.get("data", []) if isinstance(res, dict) else []

    async def _networks(self, target: NetworkTarget, site_ref: str) -> list[dict[str, Any]]:
        out = []
        for n in await self._legacy(target, site_ref, "GET", "/rest/networkconf"):
            subnet = n.get("ip_subnet")
            iface = None
            if subnet:
                try:
                    iface = ipaddress.ip_interface(subnet)
                except ValueError:
                    iface = None
            out.append({
                "id": n.get("_id"), "name": n.get("name"), "vlan": n.get("vlan"), "purpose": n.get("purpose"),
                "subnet": str(iface.network) if iface else None, "gateway": str(iface.ip) if iface else None,
                "dhcp_start": n.get("dhcpd_start"), "dhcp_stop": n.get("dhcpd_stop"), "_iface": iface,
            })
        return out

    @staticmethod
    def _pick_network(networks: list[dict[str, Any]], query: str | None) -> dict[str, Any] | None:
        if query is None:
            return None
        q = str(query).strip().lower()
        for n in networks:
            if q in {str(n["id"]).lower(), str(n["name"] or "").lower(), str(n["vlan"] or "")} or (q == "1" and n["vlan"] in (None, "", 1)):
                return n
        raise ReservationError(f"network {query!r} not found; networks: "
                               + ", ".join(f"{n['name']} (vlan {n['vlan'] or 1}, {n['subnet']})" for n in networks))

    @staticmethod
    def _network_for_ip(networks: list[dict[str, Any]], ip: ipaddress.IPv4Address) -> dict[str, Any] | None:
        hits = [n for n in networks if n["_iface"] is not None and ip in n["_iface"].network]
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def _public(n: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if n is None else {k: v for k, v in n.items() if not k.startswith("_")}

    # ------------------------------------------------------------------ read

    async def list_fixed(self, console: str | None, site: str | None, network: str | None = None,
                         include_names: bool = False) -> dict[str, Any]:
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        networks = await self._networks(target, site_ref)
        by_id = {n["id"]: n for n in networks}
        only = self._pick_network(networks, network)
        rows = []
        for u in await self._legacy(target, site_ref, "GET", "/rest/user"):
            fixed = bool(u.get("use_fixedip")) and u.get("fixed_ip")
            if not fixed and not (include_names and u.get("name")):
                continue
            net = by_id.get(u.get("network_id"))
            if only and (not fixed or net is not only):
                continue
            rows.append({"mac": normalize_mac(u.get("mac")), "name": u.get("name"), "hostname": u.get("hostname"),
                         "fixed_ip": u.get("fixed_ip") if fixed else None, "network": net["name"] if net else None})
        rows.sort(key=lambda r: (r["fixed_ip"] is None, ipaddress.ip_address(r["fixed_ip"]) if r["fixed_ip"] else 0, r["mac"] or ""))
        return {"console": target.label, "site": site_name, "network": self._public(only),
                "count": len(rows), "clients": rows}

    # ------------------------------------------------------------------ write (plan, then apply)

    async def set_many(self, items: list[dict[str, Any]], console: str | None, site: str | None,
                       network: str | None, confirm: bool) -> dict[str, Any]:
        if not items:
            raise ReservationError("give at least one item {mac, ip?, name?}")
        if len(items) > MAX_BATCH:
            raise ReservationError(f"at most {MAX_BATCH} items per call")
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        networks = await self._networks(target, site_ref)
        forced = self._pick_network(networks, network)
        users = await self._legacy(target, site_ref, "GET", "/rest/user")
        by_mac = {normalize_mac(u.get("mac")): u for u in users if normalize_mac(u.get("mac"))}
        taken = {u["fixed_ip"]: normalize_mac(u.get("mac")) for u in users if u.get("use_fixedip") and u.get("fixed_ip")}

        plan, errors, seen_macs, seen_ips = [], [], set(), {}
        for i, item in enumerate(items):
            raw_mac = item.get("mac")
            mac = normalize_mac(raw_mac)
            label = f"item {i + 1} ({raw_mac})"
            if not mac:
                errors.append(f"{label}: not a MAC address"); continue
            if mac in seen_macs:
                errors.append(f"{label}: MAC listed twice"); continue
            seen_macs.add(mac)
            ip_raw, name, clear = item.get("ip"), item.get("name"), bool(item.get("clear_fixed_ip"))
            if ip_raw is None and name is None and not clear:
                errors.append(f"{label}: nothing to change (give ip, name or clear_fixed_ip)"); continue
            if ip_raw is not None and clear:
                errors.append(f"{label}: ip and clear_fixed_ip together"); continue
            user = by_mac.get(mac)
            change: dict[str, Any] = {}
            net = None
            if ip_raw is not None:
                try:
                    ip = ipaddress.ip_address(str(ip_raw).strip())
                except ValueError:
                    errors.append(f"{label}: {ip_raw!r} is not an IP address"); continue
                net = forced or self._network_for_ip(networks, ip)
                if net is None or net["_iface"] is None:
                    errors.append(f"{label}: no single network contains {ip}; pass network="); continue
                iface = net["_iface"]
                if ip not in iface.network:
                    errors.append(f"{label}: {ip} is outside {net['name']} ({iface.network})"); continue
                if ip in (iface.network.network_address, iface.network.broadcast_address, iface.ip):
                    errors.append(f"{label}: {ip} is the network, broadcast or gateway address of {net['name']}"); continue
                if str(ip) in seen_ips:
                    errors.append(f"{label}: {ip} also given to {seen_ips[str(ip)]}"); continue
                owner = taken.get(str(ip))
                if owner and owner != mac:
                    errors.append(f"{label}: {ip} is already reserved for {owner}"); continue
                seen_ips[str(ip)] = mac
                if not (user and user.get("use_fixedip") and user.get("fixed_ip") == str(ip) and user.get("network_id") == net["id"]):
                    change.update({"use_fixedip": True, "fixed_ip": str(ip), "network_id": net["id"]})
            if clear and user and user.get("use_fixedip"):
                change["use_fixedip"] = False
            if name is not None and (user or {}).get("name") != name:
                change["name"] = name
            before = {"name": (user or {}).get("name"), "hostname": (user or {}).get("hostname"),
                      "fixed_ip": (user or {}).get("fixed_ip") if (user or {}).get("use_fixedip") else None}
            after = {"name": change.get("name", before["name"]),
                     "fixed_ip": None if change.get("use_fixedip") is False else change.get("fixed_ip", before["fixed_ip"])}
            plan.append({"mac": mac, "action": "none" if not change else ("update" if user else "create"),
                         "network": net["name"] if net else None, "before": before, "after": after,
                         "_user": user, "_change": change})

        changes = [p for p in plan if p["action"] != "none"]
        summary = {"console": target.label, "site": site_name, "items": len(items), "changes": len(changes),
                   "unchanged": len(plan) - len(changes), "errors": errors}
        public_plan = [{k: v for k, v in p.items() if not k.startswith("_")} for p in changes]
        if errors:
            return {"dry_run": True, **summary, "plan": public_plan,
                    "next": "Fix the errors; nothing is written while any item has an error."}
        if not changes:
            return {"dry_run": not confirm, **summary, "plan": [], "next": "Nothing to change."}
        if not self.settings.allow_writes:
            return {"dry_run": True, **summary, "plan": public_plan,
                    "next": "Writes are disabled; restart the server with UNIFI_ALLOW_WRITES=true to apply."}
        if not confirm:
            return {"dry_run": True, **summary, "plan": public_plan,
                    "next": "Show this plan to the user; re-run the same call with confirm=true to apply it."}

        results = []
        for p in changes:
            try:
                if p["_user"]:
                    await self._legacy(target, site_ref, "PUT", f"/rest/user/{p['_user']['_id']}", p["_change"])
                else:
                    await self._legacy(target, site_ref, "POST", "/rest/user", {"mac": p["mac"], **p["_change"]})
                results.append({"mac": p["mac"], "ok": True, **p["after"]})
            except Exception as exc:  # keep going; report per item
                results.append({"mac": p["mac"], "ok": False, "error": str(exc)})
        failed = [r for r in results if not r["ok"]]
        return {"executed": True, **summary, "applied": len(results) - len(failed), "failed": len(failed), "results": results,
                "note": "A fixed IP takes effect at the client's next DHCP request (renew, reconnect or reboot)."}

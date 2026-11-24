"""Access-point radio settings (transmit power, channel, channel width) through the classic API.

The official Network Integration API can only restart an AP. Radio configuration lives in the
classic ``rest/device`` record as ``radio_table`` (one entry per radio: ``radio`` = ``ng`` 2.4 GHz,
``na`` 5 GHz, ``6e`` 6 GHz), which the api.ui.com Connector proxies with the same Site Manager key.

A write PUTs the AP's full ``radio_table`` with only the requested radio fields changed. Every plan
shows the radio's stored entry as-is (``stored``) so field names can be checked on a live
controller before anything is written.
"""

from __future__ import annotations

from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget
from .config import Settings
from .locate import normalize_mac
from .resolve import Resolver

RADIO_BY_BAND = {"2.4": "ng", "5": "na", "6": "6e"}
BAND_BY_RADIO = {v: k for k, v in RADIO_BY_BAND.items()}
POWER_MODES = {"auto", "high", "medium", "low"}
WIDTHS = {"ng": {20, 40}, "na": {20, 40, 80, 160}, "6e": {20, 40, 80, 160, 320}}
CHANNELS = {"ng": range(1, 15), "na": range(36, 178), "6e": range(1, 234)}
STORED_KEYS = ("radio", "channel", "ht", "tx_power_mode", "tx_power", "min_rssi_enabled", "min_rssi")


class RadioError(ValueError):
    pass


def band_key(value: Any) -> str:
    """'2.4', 2.4, '2.4GHz', 'ng' -> 'ng'; 5 -> 'na'; 6 -> '6e'."""
    s = str(value).strip().lower().replace("ghz", "").strip()
    if s in BAND_BY_RADIO:
        return s
    try:
        f = float(s)
    except ValueError:
        raise RadioError(f"band {value!r}: use 2.4, 5 or 6") from None
    for band, radio in RADIO_BY_BAND.items():
        if abs(f - float(band)) < 0.01:
            return radio
    raise RadioError(f"band {value!r}: use 2.4, 5 or 6")


class APRadios:
    def __init__(self, settings: Settings, resolver: Resolver, network: NetworkAPI):
        self.settings = settings
        self.resolver = resolver
        self.network = network

    async def _site_ref(self, target: NetworkTarget, site: str | None) -> tuple[str, str]:
        site_id = await self.resolver.site_id(target, site)
        for s in await self.resolver.sites(target):
            if s["id"] == site_id:
                return s["internalReference"], s.get("name") or s["internalReference"]
        raise RadioError(f"site {site!r} not found")

    async def _legacy(self, target: NetworkTarget, site_ref: str, method: str, path: str, body: Any = None) -> list[dict[str, Any]]:
        res = await self.network.request(target, method, f"/s/{site_ref}{path}", body=body, app=NETWORK_LEGACY_APP)
        return res.get("data", []) if isinstance(res, dict) else []

    @staticmethod
    def _find_ap(devices: list[dict[str, Any]], query: str) -> dict[str, Any]:
        q = str(query).strip()
        mac = normalize_mac(q)
        aps = [d for d in devices if d.get("radio_table")]
        hits = [d for d in aps if d.get("_id") == q or (mac and normalize_mac(d.get("mac")) == mac)]
        if not hits:
            hits = [d for d in aps if str(d.get("name") or "").lower() == q.lower()]
        if not hits:
            hits = [d for d in aps if q.lower() in str(d.get("name") or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise RadioError(f"no access point matches {query!r}")
        raise RadioError(f"{query!r} matches several access points: " + ", ".join(repr(d.get("name")) for d in hits))

    async def _ap(self, target: NetworkTarget, site_ref: str, device: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(stat/device record for live stats, stored rest/device config for writing)."""
        stat = self._find_ap(await self._legacy(target, site_ref, "GET", "/stat/device"), device)
        stored = await self._legacy(target, site_ref, "GET", f"/rest/device/{stat['_id']}")
        cfg = stored[0] if stored else stat
        if not cfg.get("radio_table"):
            cfg = stat
        return stat, cfg

    @staticmethod
    def _live(stat: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out = {}
        for r in stat.get("radio_table_stats") or []:
            out[r.get("name") or r.get("radio")] = r
        by_radio = {}
        for r in stat.get("radio_table") or []:
            live = out.get(r.get("name")) or {}
            by_radio[r.get("radio")] = {"channel": live.get("channel"), "tx_power_dbm": live.get("tx_power"),
                                        "clients": live.get("num_sta"), "airtime_pct": live.get("cu_total")}
        return by_radio

    @staticmethod
    def _view(entry: dict[str, Any]) -> dict[str, Any]:
        return {k: entry.get(k) for k in STORED_KEYS if k in entry}

    # ------------------------------------------------------------------ read

    async def list_radios(self, device: str, console: str | None, site: str | None) -> dict[str, Any]:
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        stat, cfg = await self._ap(target, site_ref, device)
        live = self._live(stat)
        radios = [{"band_ghz": BAND_BY_RADIO.get(r.get("radio"), r.get("radio")), "stored": self._view(r),
                   "live": live.get(r.get("radio"))} for r in cfg.get("radio_table") or []]
        return {"console": target.label, "site": site_name, "device": stat.get("name"), "model": stat.get("model"),
                "mac": normalize_mac(stat.get("mac")), "radios": radios}

    # ------------------------------------------------------------------ write (plan, then apply)

    async def set_radios(self, device: str, radios: list[dict[str, Any]], console: str | None, site: str | None,
                         confirm: bool) -> dict[str, Any]:
        if not radios:
            raise RadioError("give at least one radio {band, tx_power?, channel?, width?}")
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        stat, cfg = await self._ap(target, site_ref, device)
        table = [dict(r) for r in cfg.get("radio_table") or []]
        by_radio = {r.get("radio"): r for r in table}
        live = self._live(stat)

        plan, errors, seen = [], [], set()
        for i, item in enumerate(radios):
            label = f"item {i + 1} (band {item.get('band')})"
            try:
                radio = band_key(item.get("band"))
            except RadioError as exc:
                errors.append(f"{label}: {exc}"); continue
            if radio not in by_radio:
                errors.append(f"{label}: {stat.get('name')} has no {BAND_BY_RADIO[radio]} GHz radio "
                              f"(has {', '.join(BAND_BY_RADIO.get(r, r) for r in by_radio)})"); continue
            if radio in seen:
                errors.append(f"{label}: band listed twice"); continue
            seen.add(radio)
            wants = {k for k in ("tx_power", "channel", "width") if item.get(k) is not None}
            if not wants:
                errors.append(f"{label}: nothing to change (tx_power, channel or width)"); continue
            change: dict[str, Any] = {}
            try:
                if "tx_power" in wants:
                    p = item["tx_power"]
                    if isinstance(p, str) and p.strip().lower() in POWER_MODES:
                        change["tx_power_mode"] = p.strip().lower()
                    else:
                        try:
                            dbm = int(p)
                        except (TypeError, ValueError):
                            raise RadioError(f"tx_power must be auto/high/medium/low or dBm, not {p!r}") from None
                        if not 1 <= dbm <= 30:
                            raise RadioError(f"tx_power {dbm} dBm out of range 1-30")
                        change.update({"tx_power_mode": "custom", "tx_power": str(dbm)})
                if "channel" in wants:
                    c = item["channel"]
                    if str(c).strip().lower() == "auto":
                        change["channel"] = "auto"
                    else:
                        try:
                            ch = int(c)
                        except (TypeError, ValueError):
                            raise RadioError(f"channel must be 'auto' or a number, not {c!r}") from None
                        if ch not in CHANNELS[radio]:
                            raise RadioError(f"channel {ch} is not a {BAND_BY_RADIO[radio]} GHz channel")
                        change["channel"] = str(ch)
                if "width" in wants:
                    try:
                        w = int(str(item["width"]).lower().replace("mhz", "").replace("ht", ""))
                    except ValueError:
                        raise RadioError(f"width must be a number of MHz, not {item['width']!r}") from None
                    if w not in WIDTHS[radio]:
                        raise RadioError(f"width {w} not allowed on {BAND_BY_RADIO[radio]} GHz ({sorted(WIDTHS[radio])})")
                    change["ht"] = w
            except RadioError as exc:
                errors.append(f"{label}: {exc}"); continue
            entry = by_radio[radio]
            if all(str(entry.get(k)) == str(v) for k, v in change.items()):
                plan.append({"band_ghz": BAND_BY_RADIO[radio], "action": "none", "_radio": radio, "_change": {}})
                continue
            plan.append({"band_ghz": BAND_BY_RADIO[radio], "action": "update", "stored": self._view(entry),
                         "after": self._view({**entry, **change}), "live": live.get(radio),
                         "_radio": radio, "_change": change})

        changes = [p for p in plan if p["action"] != "none"]
        summary = {"console": target.label, "site": site_name, "device": stat.get("name"), "model": stat.get("model"),
                   "radios": len(radios), "changes": len(changes), "unchanged": len(plan) - len(changes), "errors": errors}
        public = [{k: v for k, v in p.items() if not k.startswith("_")} for p in changes]
        if errors:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Fix the errors; nothing is written while any radio has an error."}
        if not changes:
            return {"dry_run": not confirm, **summary, "plan": [], "next": "Nothing to change."}
        if not self.settings.allow_writes:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Writes are disabled; restart the server with UNIFI_ALLOW_WRITES=true to apply."}
        if not confirm:
            return {"dry_run": True, **summary, "plan": public,
                    "next": "Check 'stored' looks like this controller's radio fields; show the plan to the user and "
                            "re-run with confirm=true to apply (the AP re-provisions; clients may briefly reconnect)."}

        for p in changes:
            by_radio[p["_radio"]].update(p["_change"])
        await self._legacy(target, site_ref, "PUT", f"/rest/device/{stat['_id']}", {"radio_table": table})
        return {"executed": True, **summary, "applied": [{"band_ghz": p["band_ghz"], **p["after"]} for p in changes],
                "note": "The AP re-provisions (a few seconds; clients may reconnect). Verify with list_ap_radios / "
                        "device_health after a minute."}

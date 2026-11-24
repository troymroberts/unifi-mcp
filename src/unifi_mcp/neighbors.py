"""Neighbouring access points each AP hears (classic ``stat/rogueap``), split into own vs foreign.

Every UniFi AP scans and reports the BSSIDs it hears with channel and signal. That is the data needed to
see which radios contribute co-channel interference: own APs heard loudly on the same channel are
candidates for lower transmit power or a different channel; loud foreign APs mean "move away from
that channel". Own BSSIDs are recognised from every adopted AP's ``vap_table``. Read-only.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .client import NETWORK_LEGACY_APP, NetworkAPI, NetworkTarget
from .config import Settings
from .locate import normalize_mac
from .resolve import Resolver

BAND_BY_RADIO = {"ng": "2.4", "na": "5", "6e": "6"}


class NeighborError(ValueError):
    pass


def _band(row: dict[str, Any]) -> str | None:
    radio = row.get("radio") or row.get("radio_name")
    if radio in BAND_BY_RADIO:
        return BAND_BY_RADIO[radio]
    try:
        freq = int(row.get("freq") or 0)
    except (TypeError, ValueError):
        freq = 0
    if freq:
        return "2.4" if freq < 3000 else ("6" if freq >= 5925 else "5")
    try:
        ch = int(row.get("channel") or 0)
    except (TypeError, ValueError):
        return None
    return "2.4" if 1 <= ch <= 14 else ("5" if ch >= 32 else None)


def _signal(row: dict[str, Any]) -> int | None:
    """Neighbour signal in dBm: 'signal' when given, else UniFi's rssi (SNR-style, ~signal+95)."""
    for key in ("signal", "rssi"):
        v = row.get(key)
        if v is None:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        return n if n < 0 else n - 95
    return None


def _seen(row: dict[str, Any], now: float) -> tuple[str | None, int | None]:
    """(last_seen ISO-8601 UTC, age in seconds) from classic 'last_seen' (epoch s or ms) or 'age' (s)."""
    ts = row.get("last_seen")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts = None
    if ts and ts > 1e12:  # milliseconds
        ts /= 1000
    if ts:
        iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return iso, max(0, int(now - ts))
    try:
        age = int(row["age"])
    except (KeyError, TypeError, ValueError):
        return None, None
    iso = datetime.fromtimestamp(now - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso, age


class APNeighbors:
    def __init__(self, settings: Settings, resolver: Resolver, network: NetworkAPI):
        self.settings = settings
        self.resolver = resolver
        self.network = network

    async def _site_ref(self, target: NetworkTarget, site: str | None) -> tuple[str, str]:
        site_id = await self.resolver.site_id(target, site)
        for s in await self.resolver.sites(target):
            if s["id"] == site_id:
                return s["internalReference"], s.get("name") or s["internalReference"]
        raise NeighborError(f"site {site!r} not found")

    async def _legacy(self, target: NetworkTarget, site_ref: str, method: str, path: str, body: Any = None) -> list[dict[str, Any]]:
        res = await self.network.request(target, method, f"/s/{site_ref}{path}", body=body, app=NETWORK_LEGACY_APP)
        return res.get("data", []) if isinstance(res, dict) else []

    @staticmethod
    def _ap_channels(ap: dict[str, Any]) -> dict[str, Any]:
        names = {r.get("name"): r.get("radio") for r in ap.get("radio_table") or []}
        out = {}
        for r in ap.get("radio_table_stats") or []:
            band = BAND_BY_RADIO.get(r.get("radio") or names.get(r.get("name")))
            if band and r.get("channel") not in (None, 0):
                out[band] = {"channel": r.get("channel"), "tx_power_dbm": r.get("tx_power"),
                             "airtime_pct": r.get("cu_total")}
        return out

    async def neighbors(self, console: str | None, site: str | None, device: str | None = None,
                        band: str | float | None = None, same_channel_only: bool = False,
                        min_signal: int | None = None, limit: int = 40,
                        max_age_s: int | None = None) -> dict[str, Any]:
        target = await self.resolver.console(console)
        site_ref, site_name = await self._site_ref(target, site)
        devices = await self._legacy(target, site_ref, "GET", "/stat/device")
        aps = [d for d in devices if d.get("radio_table")]
        by_mac = {normalize_mac(a.get("mac")): a for a in aps}
        own_bssid: dict[str, str] = {}
        own_ssids: set[str] = set()
        for a in aps:
            for v in a.get("vap_table") or []:
                b = normalize_mac(v.get("bssid"))
                if b:
                    own_bssid[b] = a.get("name") or normalize_mac(a.get("mac"))
                if v.get("essid"):
                    own_ssids.add(v["essid"])
        want_band = None
        if band is not None:
            want_band = str(band).lower().replace("ghz", "").strip()
            if want_band not in ("2.4", "5", "6"):
                raise NeighborError("band must be 2.4, 5 or 6")

        only = None
        if device:
            q = str(device).strip()
            mac = normalize_mac(q)
            hits = [a for a in aps if a.get("_id") == q or (mac and normalize_mac(a.get("mac")) == mac)] \
                or [a for a in aps if str(a.get("name") or "").lower() == q.lower()] \
                or [a for a in aps if q.lower() in str(a.get("name") or "").lower()]
            if len(hits) != 1:
                raise NeighborError(f"no single access point matches {device!r}"
                                    + (": " + ", ".join(repr(h.get("name")) for h in hits) if hits else ""))
            only = hits[0]

        heard: dict[str, list[dict[str, Any]]] = defaultdict(list)
        now = time.time()
        for row in await self._legacy(target, site_ref, "GET", "/stat/rogueap"):
            rep = normalize_mac(row.get("ap_mac"))
            ap = by_mac.get(rep)
            if ap is None or (only is not None and ap is not only):
                continue
            nb_band = _band(row)
            if want_band and nb_band != want_band:
                continue
            sig = _signal(row)
            if min_signal is not None and (sig is None or sig < min_signal):
                continue
            last_seen, age_s = _seen(row, now)
            if max_age_s is not None and age_s is not None and age_s > max_age_s:
                continue
            mine = self._ap_channels(ap).get(nb_band or "", {})
            try:
                same = mine.get("channel") is not None and int(row.get("channel")) == int(mine["channel"])
            except (TypeError, ValueError):
                same = False
            if same_channel_only and not same:
                continue
            bssid = normalize_mac(row.get("bssid"))
            own = own_bssid.get(bssid)
            heard[ap.get("name") or rep].append({
                "bssid": bssid, "essid": row.get("essid"), "band_ghz": nb_band, "channel": row.get("channel"),
                "signal_dbm": sig, "same_channel": same, "last_seen": last_seen, "age_s": age_s,
                "own_ap": own, "likely_own": bool(not own and row.get("is_ubnt") and row.get("essid") in own_ssids),
                "foreign": not own and not (row.get("is_ubnt") and row.get("essid") in own_ssids),
            })

        def ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(rows, key=lambda r: (r["signal_dbm"] is None, -(r["signal_dbm"] or -999)))

        if only is not None:
            rows = ordered(heard.get(only.get("name") or normalize_mac(only.get("mac")), []))
            return {"console": target.label, "site": site_name, "device": only.get("name"),
                    "own_channels": self._ap_channels(only), "heard": len(rows), "neighbors": rows[:limit],
                    "note": "signal_dbm stronger than about -70 on the same channel is real co-channel contention. "
                            "own_ap = your AP (tune its power/channel); foreign = move your AP's channel instead."}

        summary, loud = [], defaultdict(list)
        for ap in aps:
            name = ap.get("name") or normalize_mac(ap.get("mac"))
            rows = heard.get(name, [])
            same = [r for r in rows if r["same_channel"]]
            for r in same:
                if r["own_ap"] and r["signal_dbm"] is not None:
                    loud[r["own_ap"]].append((r["signal_dbm"], name, r["band_ghz"]))
            strongest = ordered(same)[:1]
            summary.append({
                "ap": name, "own_channels": self._ap_channels(ap), "heard": len(rows),
                "same_channel_own": sum(1 for r in same if r["own_ap"] or r["likely_own"]),
                "same_channel_foreign": sum(1 for r in same if r["foreign"]),
                "strongest_same_channel": strongest[0] if strongest else None,
            })
        summary.sort(key=lambda s: -(s["same_channel_own"] + s["same_channel_foreign"]))
        loudest = sorted(
            ({"own_ap": k, "heard_by_same_channel": len(v), "strongest_dbm": max(x[0] for x in v),
              "heard_by": [f"{n} ({b} GHz) {s} dBm" for s, n, b in sorted(v, reverse=True)[:8]]}
             for k, v in loud.items()),
            key=lambda r: (-r["heard_by_same_channel"], -r["strongest_dbm"]))
        return {"console": target.label, "site": site_name, "band_ghz": want_band, "aps": summary,
                "loudest_own_aps": loudest,
                "note": "loudest_own_aps = your APs heard on the same channel by the most other APs (and how "
                        "loudly): candidates for lower transmit power or a different channel."}

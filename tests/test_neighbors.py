import time

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unifi_mcp.config import Settings
from unifi_mcp.server import create_server

from test_server import HOST_OWNED, NET_SITES, call


def ap(id_, mac, name, ch24, bssid24, ch5=36, bssid5=None):
    return {"_id": id_, "mac": mac, "name": name, "model": "U7PRO",
            "radio_table": [{"name": "wifi0", "radio": "ng"}, {"name": "wifi1", "radio": "na"}],
            "radio_table_stats": [{"name": "wifi0", "channel": ch24, "tx_power": 23, "cu_total": 90},
                                  {"name": "wifi1", "channel": ch5, "tx_power": 24, "cu_total": 8}],
            "vap_table": [{"bssid": bssid24, "essid": "BSLTT", "radio": "ng"},
                          {"bssid": bssid5 or bssid24[:-1] + "f", "essid": "BSLTT", "radio": "na"}]}


A = ap("a", "aa:00:00:00:00:01", "Downstairs Sales U7 Pro", 6, "aa:00:00:00:10:01")
B = ap("b", "aa:00:00:00:00:02", "Paints AP U7 Pro", 6, "aa:00:00:00:10:02")
C = ap("c", "aa:00:00:00:00:03", "Store AP1", 1, "aa:00:00:00:10:03")
SWITCH = {"_id": "sw", "mac": "bb:00:00:00:00:01", "name": "Core 1", "port_table": []}
ROGUE = [
    {"ap_mac": A["mac"], "bssid": B["vap_table"][0]["bssid"], "essid": "BSLTT", "channel": 6, "radio": "ng",
     "signal": -55, "is_ubnt": True},
    {"ap_mac": A["mac"], "bssid": "cc:00:00:00:00:09", "essid": "ShopWifi", "channel": 6, "radio": "ng",
     "rssi": 20, "is_ubnt": False},                                      # rssi 20 -> -75 dBm
    {"ap_mac": A["mac"], "bssid": C["vap_table"][0]["bssid"], "essid": "BSLTT", "channel": 1, "radio": "ng",
     "signal": -60, "is_ubnt": True},
    {"ap_mac": A["mac"], "bssid": "dd:00:00:00:00:01", "essid": "BSLTT", "channel": 6, "freq": 2437,
     "signal": -80, "is_ubnt": True},                                    # Ubiquiti on our SSID, not adopted
    {"ap_mac": B["mac"], "bssid": A["vap_table"][0]["bssid"], "essid": "BSLTT", "channel": 6, "radio": "ng",
     "signal": -58, "is_ubnt": True},
    {"ap_mac": C["mac"], "bssid": A["vap_table"][0]["bssid"], "essid": "BSLTT", "channel": 6, "radio": "ng",
     "signal": -50, "is_ubnt": True},                                    # different channel for C
    {"ap_mac": "ee:00:00:00:00:01", "bssid": "x", "channel": 6},       # reporter not adopted: ignored
]


class FakeConsole:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ok = lambda data: httpx.Response(200, json={"meta": {"rc": "ok"}, "data": data})  # noqa: E731
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED]})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES)
        if path.endswith("/s/default/stat/device"):
            return ok([A, B, C, SWITCH])
        if path.endswith("/s/default/stat/rogueap"):
            return ok(ROGUE)
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


def srv():
    return create_server(Settings(sm_api_key="k"), transport=httpx.MockTransport(FakeConsole()))


ARGS = dict(console="Office-UDM", site="Default")


async def test_one_ap_strongest_first_with_own_and_foreign():
    r = await call(srv(), "list_ap_neighbors", device="downstairs", **ARGS)
    assert r["device"] == "Downstairs Sales U7 Pro"
    assert r["own_channels"]["2.4"]["channel"] == 6
    n = r["neighbors"]
    assert [x["signal_dbm"] for x in n] == [-55, -60, -75, -80]
    assert n[0]["own_ap"] == "Paints AP U7 Pro" and n[0]["same_channel"] is True
    assert n[1]["own_ap"] == "Store AP1" and n[1]["same_channel"] is False
    assert n[2]["foreign"] is True and n[2]["essid"] == "ShopWifi"
    assert n[3]["likely_own"] is True and n[3]["band_ghz"] == "2.4"


async def test_filters_same_channel_and_min_signal():
    r = await call(srv(), "list_ap_neighbors", device="Downstairs Sales U7 Pro", same_channel_only=True,
                   min_signal=-76, **ARGS)
    assert [x["bssid"] for x in r["neighbors"]] == ["aa:00:00:00:10:02", "cc:00:00:00:00:09"]


async def test_site_summary_and_loudest_own_aps():
    r = await call(srv(), "list_ap_neighbors", band="2.4", **ARGS)
    rows = {a["ap"]: a for a in r["aps"]}
    assert rows["Downstairs Sales U7 Pro"]["same_channel_own"] == 2   # Paints + likely-own on BSLTT
    assert rows["Downstairs Sales U7 Pro"]["same_channel_foreign"] == 1
    assert rows["Store AP1"]["same_channel_own"] == 0                  # hears A, but on another channel
    loud = {x["own_ap"]: x for x in r["loudest_own_aps"]}
    assert loud["Paints AP U7 Pro"]["strongest_dbm"] == -55
    assert loud["Downstairs Sales U7 Pro"]["heard_by_same_channel"] == 1


async def test_bad_band_and_unknown_device():
    with pytest.raises(ToolError, match="band must be"):
        await call(srv(), "list_ap_neighbors", band="7", **ARGS)
    with pytest.raises(ToolError, match="no single access point"):
        await call(srv(), "list_ap_neighbors", device="Core 1", **ARGS)


class AgedConsole(FakeConsole):
    """Same APs; rogue rows carry last_seen (epoch s / ms) or age, like live controllers."""

    def __init__(self):
        now = time.time()
        self.rows = [
            {**ROGUE[0], "last_seen": int(now - 30)},           # fresh, epoch seconds
            {**ROGUE[1], "age": 4000},                          # stale, age only
            {**ROGUE[3], "last_seen": int((now - 120) * 1000)},  # epoch milliseconds
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/s/default/stat/rogueap"):
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": self.rows})
        return super().__call__(request)


async def test_last_seen_age_and_max_age_filter():
    s = create_server(Settings(sm_api_key="k"), transport=httpx.MockTransport(AgedConsole()))
    r = await call(s, "list_ap_neighbors", device="Downstairs Sales U7 Pro", **ARGS)
    by_ssid = {n["essid"] if n["essid"] != "BSLTT" or n["own_ap"] else "likely": n for n in r["neighbors"]}
    assert 25 <= by_ssid["BSLTT"]["age_s"] <= 40 and by_ssid["BSLTT"]["last_seen"].endswith("Z")
    assert by_ssid["ShopWifi"]["age_s"] == 4000
    assert 110 <= by_ssid["likely"]["age_s"] <= 135
    fresh = await call(s, "list_ap_neighbors", device="Downstairs Sales U7 Pro", max_age_s=600, **ARGS)
    assert {n["essid"] for n in fresh["neighbors"]} == {"BSLTT"} and len(fresh["neighbors"]) == 2

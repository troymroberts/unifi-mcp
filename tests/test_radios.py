import copy
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unifi_mcp.config import Settings
from unifi_mcp.server import create_server

from test_server import HOST_OWNED, NET_SITES, call

RADIO_TABLE = [
    {"name": "wifi0", "radio": "ng", "channel": "6", "ht": 20, "tx_power_mode": "high", "min_rssi_enabled": False},
    {"name": "wifi1", "radio": "na", "channel": "auto", "ht": 80, "tx_power_mode": "auto"},
    {"name": "wifi2", "radio": "6e", "channel": "auto", "ht": 160, "tx_power_mode": "auto"},
]
STATS = [{"name": "wifi0", "channel": 6, "tx_power": 23, "num_sta": 12, "cu_total": 96},
         {"name": "wifi1", "channel": 140, "tx_power": 24, "num_sta": 41, "cu_total": 8},
         {"name": "wifi2", "channel": 37, "tx_power": 21, "num_sta": 0, "cu_total": 1}]


class FakeConsole:
    def __init__(self):
        self.ap = {"_id": "dev-u7", "mac": "a8:9c:6c:50:7b:28", "name": "Downstairs Sales U7 Pro", "model": "U7PRO",
                   "radio_table": copy.deepcopy(RADIO_TABLE), "radio_table_stats": STATS}
        self.switch = {"_id": "dev-sw", "mac": "aa:00:00:00:00:01", "name": "Downstairs Switch", "port_table": []}
        self.puts: list[dict] = []
        self.rest_get_404 = False  # like a live console that won't serve GET rest/device/<id>

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ok = lambda data: httpx.Response(200, json={"meta": {"rc": "ok"}, "data": data})  # noqa: E731
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED]})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES)
        if path.endswith("/s/default/stat/device"):
            return ok([self.ap, self.switch])
        if path.endswith("/s/default/rest/device/dev-u7"):
            if request.method == "PUT":
                body = json.loads(request.content)
                self.puts.append(body)
                return ok([body])
            if self.rest_get_404:
                return httpx.Response(404, json={"message": "Not Found"})
            return ok([{"_id": "dev-u7", "radio_table": copy.deepcopy(self.ap["radio_table"])}])
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


def make(fake, writes=True):
    return create_server(Settings(sm_api_key="k", allow_writes=writes), transport=httpx.MockTransport(fake))


ARGS = dict(console="Office-UDM", site="Default")


async def test_list_ap_radios_stored_and_live():
    r = await call(make(FakeConsole()), "list_ap_radios", device="downstairs sales", **ARGS)
    bands = {x["band_ghz"]: x for x in r["radios"]}
    assert set(bands) == {"2.4", "5", "6"}
    assert bands["2.4"]["stored"]["tx_power_mode"] == "high"
    assert bands["2.4"]["live"] == {"channel": 6, "tx_power_dbm": 23, "clients": 12, "airtime_pct": 96}


async def test_low_power_on_24_dry_run_then_apply_keeps_other_bands():
    fake = FakeConsole()
    plan = await call(make(fake), "set_ap_radios", device="Downstairs Sales U7 Pro",
                      radios=[{"band": 2.4, "tx_power": "low"}], **ARGS)
    assert plan["dry_run"] is True and plan["changes"] == 1 and fake.puts == []
    p = plan["plan"][0]
    assert p["stored"]["tx_power_mode"] == "high" and p["after"]["tx_power_mode"] == "low"

    done = await call(make(fake), "set_ap_radios", device="Downstairs Sales U7 Pro", confirm=True,
                      radios=[{"band": "2.4GHz", "tx_power": "low"}], **ARGS)
    assert done["executed"] is True
    table = {r["radio"]: r for r in fake.puts[0]["radio_table"]}
    assert table["ng"]["tx_power_mode"] == "low" and table["ng"]["channel"] == "6"
    assert table["na"] == RADIO_TABLE[1] and table["6e"] == RADIO_TABLE[2]


async def test_custom_dbm_channel_and_width():
    fake = FakeConsole()
    await call(make(fake), "set_ap_radios", device="dev-u7", confirm=True,
               radios=[{"band": 2.4, "tx_power": 6, "channel": 11, "width": 20}], **ARGS)
    ng = {r["radio"]: r for r in fake.puts[0]["radio_table"]}["ng"]
    assert ng["tx_power_mode"] == "custom" and ng["tx_power"] == "6" and ng["channel"] == "11" and ng["ht"] == 20


async def test_validation_errors_write_nothing():
    fake = FakeConsole()
    r = await call(make(fake), "set_ap_radios", device="Downstairs Sales U7 Pro", confirm=True,
                   radios=[{"band": 2.4, "channel": 36}, {"band": 5, "width": 320}, {"band": 7, "tx_power": "low"},
                           {"band": 6, "tx_power": 99}, {"band": 2.4, "tx_power": "low"}], **ARGS)
    assert r["dry_run"] is True and fake.puts == []
    assert len(r["errors"]) == 5  # bad channel, bad width, bad band, bad dBm, 2.4 listed twice


async def test_no_change_and_writes_disabled():
    fake = FakeConsole()
    same = await call(make(fake), "set_ap_radios", device="Downstairs Sales U7 Pro", confirm=True,
                      radios=[{"band": 2.4, "tx_power": "high"}], **ARGS)
    assert same["changes"] == 0 and fake.puts == []
    off = await call(make(fake, writes=False), "set_ap_radios", device="Downstairs Sales U7 Pro", confirm=True,
                     radios=[{"band": 2.4, "tx_power": "low"}], **ARGS)
    assert off["dry_run"] is True and "UNIFI_ALLOW_WRITES" in off["next"] and fake.puts == []


async def test_not_an_ap_is_an_error():
    with pytest.raises(ToolError, match="no access point matches"):
        await call(make(FakeConsole()), "list_ap_radios", device="Downstairs Switch", **ARGS)


async def test_falls_back_to_stat_device_when_rest_get_is_404():
    fake = FakeConsole()
    fake.rest_get_404 = True
    listed = await call(make(fake), "list_ap_radios", device="Downstairs Sales U7 Pro", **ARGS)
    assert {r["band_ghz"] for r in listed["radios"]} == {"2.4", "5", "6"}
    done = await call(make(fake), "set_ap_radios", device="Downstairs Sales U7 Pro", confirm=True,
                      radios=[{"band": 2.4, "tx_power": "low"}], **ARGS)
    assert done["executed"] is True
    table = {r["radio"]: r for r in fake.puts[0]["radio_table"]}
    assert table["ng"]["tx_power_mode"] == "low" and table["na"] == RADIO_TABLE[1]

import httpx
import pytest

from unifi_mcp.config import ConsoleAlias, Settings
from unifi_mcp.locate import normalize_mac
from unifi_mcp.server import create_server

from test_server import HOST_OWNED, NET_SITES, call

HOST_SECOND = {**HOST_OWNED, "id": "DDD:4",
               "reportedState": {**HOST_OWNED["reportedState"], "hostname": "Branch-UDM"}}
ONE_SITE = {"offset": 0, "limit": 25, "count": 1, "totalCount": 1,
            "data": [{"id": "uuid-branch", "internalReference": "default", "name": "Default"}]}

STA = {"meta": {"rc": "ok"}, "data": [
    {"mac": "A8:A1:59:02:F5:78", "ip": "192.168.1.112", "hostname": "pc-cashier-7", "name": "PC Cashier 7",
     "is_wired": True, "sw_mac": "74:ac:b9:3e:6c:ea", "sw_port": 20, "sw_depth": 1, "network": "Default"},
    {"mac": "11:22:33:44:55:66", "ip": "192.168.1.50", "hostname": "laptop", "is_wired": False,
     "ap_mac": "e0:63:da:e3:79:04", "essid": "Office"},
]}
DEVICES = {"meta": {"rc": "ok"}, "data": [
    {"mac": "74:ac:b9:3e:6c:ea", "name": "1 - Ground Floor Main", "model": "US48P500", "type": "usw"},
    {"mac": "e0:63:da:e3:79:04", "name": "Basement AP2", "model": "U7NHD", "type": "uap"},
]}


class FakeFleet:
    def __init__(self, legacy_ok=True):
        self.legacy_ok = legacy_ok
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED, HOST_SECOND]})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES if "/AAA:1/" in path else ONE_SITE)
        if "/network/api/s/" in path:
            if not self.legacy_ok:
                return httpx.Response(404, json={"message": "not found"})
            if "/DDD:4/" in path:
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
            if path.endswith("/s/default/stat/sta"):
                return httpx.Response(200, json=STA)
            if path.endswith("/stat/sta"):
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
            if path.endswith("/stat/device-basic"):
                return httpx.Response(200, json=DEVICES)
        if path.endswith("/v1/sites/uuid-default/clients"):
            return httpx.Response(200, json={"offset": 0, "count": 1, "totalCount": 1, "data": [
                {"type": "WIRED", "macAddress": "a8:a1:59:02:f5:78", "ipAddress": "192.168.1.112",
                 "name": "PC Cashier 7", "uplinkDeviceId": "dev-sw1"}]})
        if path.endswith("/clients"):
            return httpx.Response(200, json={"offset": 0, "count": 0, "totalCount": 0, "data": []})
        if path.endswith("/v1/sites/uuid-default/devices"):
            return httpx.Response(200, json={"offset": 0, "count": 1, "totalCount": 1, "data": [
                {"id": "dev-sw1", "macAddress": "74:ac:b9:3e:6c:ea", "name": "1 - Ground Floor Main",
                 "model": "US 48 PoE", "features": ["switching"]}]})
        if path.endswith("/devices"):
            return httpx.Response(200, json={"offset": 0, "count": 0, "totalCount": 0, "data": []})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


def make(fake, **overrides):
    return create_server(Settings(sm_api_key="sm-key", **overrides), transport=httpx.MockTransport(fake))


def test_normalize_mac():
    assert normalize_mac("A8-A1-59-02-F5-78") == "a8:a1:59:02:f5:78"
    assert normalize_mac("a8a1.5902.f578") == "a8:a1:59:02:f5:78"
    assert normalize_mac("not a mac") is None
    assert normalize_mac("") is None


async def test_wired_client_resolves_switch_and_port_via_legacy_api():
    fake = FakeFleet()
    r = await call(make(fake), "find_client", macs=["A8-A1-59-02-F5-78"], console="Office-UDM", site="Default")
    assert r["found"] is True
    m = r["matches"][0]
    assert m["found_via"] == "hint" and m["matched_by"] == "mac" and m["source"] == "legacy stat/sta"
    assert m["switch"] == {"name": "1 - Ground Floor Main", "model": "US48P500", "mac": "74:ac:b9:3e:6c:ea",
                           "port": 20, "depth": 1}
    assert m["client"]["connection"] == "wired"
    assert not any("/DDD:4/" in p for p in fake.paths), "hint hit: the rest of the fleet is not searched"


async def test_wireless_client_gets_ap_and_ssid_and_is_found_by_fleet_search():
    fake = FakeFleet()
    r = await call(make(fake), "find_client", ip="192.168.1.50")
    m = r["matches"][0]
    assert m["found_via"] == "fleet search" and m["matched_by"] == "ip"
    assert m["access_point"]["name"] == "Basement AP2" and m["access_point"]["ssid"] == "Office"
    assert "switch" not in m


async def test_group_mapping_searches_that_console_first():
    fake = FakeFleet()
    aliases = {"branch": ConsoleAlias(name="branch", host="Branch-UDM", groups=("BSL PCs",)),
               "office": ConsoleAlias(name="office", host="Office-UDM", groups=("Office PCs",))}
    r = await call(make(fake, aliases=aliases), "find_client", macs=["a8:a1:59:02:f5:78"], group="office pcs",
                   search_fleet=False)
    assert r["found"] is True and r["matches"][0]["found_via"] == "group 'office pcs'"
    assert not any("/DDD:4/" in p for p in fake.paths)


async def test_falls_back_to_integration_api_without_port():
    fake = FakeFleet(legacy_ok=False)
    r = await call(make(fake), "find_client", macs=["a8:a1:59:02:f5:78"], console="Office-UDM", site="Default")
    m = r["matches"][0]
    assert m["source"] == "integration /clients"
    assert m["switch"]["name"] == "1 - Ground Floor Main" and m["switch"]["port"] is None


async def test_not_found_and_validation():
    fake = FakeFleet()
    r = await call(make(fake), "find_client", macs=["00:00:00:00:00:01", "bogus"], search_fleet=True)
    assert r["found"] is False and r["ignored_invalid_macs"] == ["bogus"]
    with pytest.raises(Exception):
        await call(make(fake), "find_client")

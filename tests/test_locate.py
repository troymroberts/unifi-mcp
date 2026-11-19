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
     "ap_mac": "e0:63:da:e3:79:04", "essid": "Office", "radio": "ng", "channel": 6,
     "signal": -67, "satisfaction": 42, "tx_rate": 72200, "network": "Voice"},
]}
DEVICES = {"meta": {"rc": "ok"}, "data": [
    {"mac": "74:ac:b9:3e:6c:ea", "name": "1 - Ground Floor Main", "model": "US48P500", "type": "usw"},
    {"mac": "e0:63:da:e3:79:04", "name": "Basement AP2", "model": "U7NHD", "type": "uap"},
    {"mac": "e0:63:da:e3:79:05", "name": "Basement AP1", "model": "U7NHD", "type": "uap"},
]}
EVENTS = {"meta": {"rc": "ok"}, "data": [
    {"key": "EVT_WU_Roam", "time": 1_900_000_000_000, "datetime": "2030-03-14T09:00:00Z",
     "user": "11:22:33:44:55:66", "ssid": "Office", "ap_from": "e0:63:da:e3:79:05",
     "ap_to": "e0:63:da:e3:79:04", "channel_from": 6, "channel_to": 36, "radio_from": "ng", "radio_to": "na"},
    {"key": "EVT_WU_Disconnected", "time": 1_900_000_100_000, "datetime": "2030-03-14T09:01:40Z",
     "user": "11:22:33:44:55:66", "ssid": "Office", "ap": "e0:63:da:e3:79:04", "duration": 512},
    {"key": "EVT_SW_PortDown", "time": 1_900_000_150_000, "datetime": "2030-03-14T09:02:30Z",
     "sw": "74:ac:b9:3e:6c:ea", "port": 20},
    {"key": "EVT_AD_Login", "time": 1_900_000_200_000, "user": "admin"},
]}
DEVICE_STAT = {"meta": {"rc": "ok"}, "data": [
    {"mac": "74:ac:b9:3e:6c:ea", "name": "1 - Ground Floor Main", "model": "US48P500", "type": "usw",
     "state": 1, "uptime": 864000, "version": "7.0.0", "general_temperature": 45,
     "system-stats": {"cpu": "7.5", "mem": "46.0"}, "sys_stats": {"loadavg_1": "0.8"},
     "port_table": [
         {"port_idx": 1, "name": "Uplink", "up": True, "speed": 1000, "stp_state": "forwarding",
          "rx_dropped": 500000, "tx_dropped": 12000},
         {"port_idx": 20, "name": "Cashier 7", "up": True, "speed": 100, "stp_state": "forwarding",
          "rx_errors": 1234, "tx_errors": 5},
         {"port_idx": 24, "name": "Loop", "up": True, "speed": 1000, "stp_state": "blocking"},
         {"port_idx": 49, "name": "SFP+ 1", "up": True, "speed": 10000, "stp_state": "forwarding",
          "rx_errors": 777, "tx_errors": 0, "sfp_found": True, "sfp_rxpower": "-18.50",
          "sfp_txpower": "-2.30", "sfp_temperature": "45.1", "sfp_vendor": "UBNT"}]},
    {"mac": "e0:63:da:e3:79:04", "name": "Basement AP2", "model": "U7NHD", "type": "uap", "state": 1,
     "uptime": 500000, "general_temperature": 52, "system-stats": {"cpu": "12.0", "mem": "58.0"},
     "radio_table_stats": [
         {"radio": "ng", "channel": 6, "num_sta": 18, "cu_total": 88, "cu_self_rx": 10, "cu_self_tx": 12,
          "tx_retries": 35, "satisfaction": -1, "tx_power": 20},
         {"radio": "na", "channel": 36, "num_sta": 5, "cu_total": 20, "cu_self_rx": 5, "cu_self_tx": 5,
          "tx_retries": 4, "satisfaction": 95}]},
    {"mac": "e0:63:da:e3:79:05", "name": "Basement AP1", "model": "U7NHD", "type": "uap", "state": 0,
     "system-stats": {"cpu": "3.0", "mem": "40.0"}},
]}
HEALTH = {"meta": {"rc": "ok"}, "data": [
    {"subsystem": "wan", "status": "ok", "latency": 3, "drops": 0, "xput_up": 50, "xput_down": 200,
     "speedtest_ping": 5, "wan_ip": "181.118.38.183"},
    {"subsystem": "wlan", "status": "warning", "num_user": 60, "num_ap": 10, "num_disconnected": 1},
    {"subsystem": "lan", "status": "ok", "num_sw": 5},
]}
STADPI = {"meta": {"rc": "ok"}, "data": [
    {"mac": "11:22:33:44:55:66",
     "by_app": [{"app": 49, "cat": 13, "application": "Instagram", "tx_bytes": 6_000_000, "rx_bytes": 300_000_000},
                {"app": 7, "cat": 19, "application": "YouTube", "tx_bytes": 1_000_000, "rx_bytes": 50_000_000}],
     "by_cat": [{"cat": 13, "category": "Social", "tx_bytes": 6_000_000, "rx_bytes": 300_000_000}]},
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
            if path.endswith("/s/default/stat/event"):
                return httpx.Response(200, json=EVENTS)
            if path.endswith("/s/default/stat/device"):
                return httpx.Response(200, json=DEVICE_STAT)
            if path.endswith("/s/default/stat/health"):
                return httpx.Response(200, json=HEALTH)
            if path.endswith("/s/default/stat/stadpi"):
                return httpx.Response(200, json=STADPI)
            if path.endswith(("/stat/event", "/stat/device", "/stat/health", "/stat/stadpi")):
                return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": []})
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
    ap = m["access_point"]
    assert ap["name"] == "Basement AP2" and ap["ssid"] == "Office"
    assert ap["band_ghz"] == 2.4 and ap["channel"] == 6 and ap["signal_dbm"] == -67 and ap["satisfaction"] == 42
    assert "switch" not in m


def test_band_ghz():
    from unifi_mcp.locate import band_ghz
    assert band_ghz("ng") == 2.4 and band_ghz("na") == 5.0 and band_ghz("6e") == 6.0
    assert band_ghz(None, 6) == 2.4 and band_ghz(None, 36) == 5.0 and band_ghz(None, None) is None


async def test_list_wireless_clients_band_experience_and_filter():
    fake = FakeFleet()
    r = await call(make(fake), "list_wireless_clients", console="Office-UDM", site="Default", band=2.4)
    assert r["count"] == 1
    c = r["clients"][0]
    assert c["mac"] == "11:22:33:44:55:66" and c["band_ghz"] == 2.4 and c["channel"] == 6
    assert c["signal_dbm"] == -67 and c["ssid"] == "Office" and c["access_point"]["name"] == "Basement AP2"
    assert (await call(make(FakeFleet()), "list_wireless_clients", console="Office-UDM", site="Default",
                       band=5))["count"] == 0


async def test_list_events_roam_disconnect_and_portdown():
    fake = FakeFleet()
    r = await call(make(fake), "list_events", console="Office-UDM", site="Default", hours=0)
    kinds = {e["kind"] for e in r["events"]}
    assert {"roam", "disconnect", "port_down"} <= kinds and "connect" not in kinds  # admin login ignored
    roam = next(e for e in r["events"] if e["kind"] == "roam")
    assert roam["from_ap"] == "Basement AP1" and roam["to_ap"] == "Basement AP2" and roam["channel_to"] == 36
    pd = next(e for e in r["events"] if e["kind"] == "port_down")
    assert pd["device"] == "1 - Ground Floor Main" and pd["port"] == 20
    only = await call(make(FakeFleet()), "list_events", console="Office-UDM", site="Default", hours=0,
                      kinds=["roam"])
    assert only["count"] == 1 and only["events"][0]["kind"] == "roam"


async def test_device_health_flags_ports_airtime_and_offline():
    fake = FakeFleet()
    r = await call(make(fake), "device_health", console="Office-UDM", site="Default", problems_only=True)
    by = {d["name"]: d for d in r["devices"]}
    sw = by["1 - Ground Floor Main"]
    assert sw["cpu_pct"] == 7.5 and any(p["port"] == 20 for p in sw["problem_ports"])
    assert any("stp-blocking" in p["issues"] for p in sw["problem_ports"])
    ap = by["Basement AP2"]
    r24 = next(rad for rad in ap["radios"] if rad["band_ghz"] == 2.4)
    assert r24["airtime_pct"] == 88 and r24["clients"] == 18 and r24["ext_interference_pct"] == 66
    assert "satisfaction" not in r24  # -1 (not computed) is dropped, not reported as a score
    assert any("airtime" in p for p in ap["problems"])
    assert "Basement AP1" in by  # state 0 -> flagged
    # drops alone must NOT flag a port (UniFi counts normal flooded frames there)
    assert all(p["port"] != 1 for p in sw["problem_ports"])
    # a flagged SFP port carries its DDM optics inline
    sfpp = next(p for p in sw["problem_ports"] if p["port"] == 49)
    assert sfpp["sfp"]["sfp_rxpower"] == "-18.50" and sfpp["sfp"]["sfp_vendor"] == "UBNT"


async def test_device_health_include_sfp_lists_optics():
    fake = FakeFleet()
    r = await call(make(fake), "device_health", console="Office-UDM", site="Default", include_sfp=True)
    sw = next(d for d in r["devices"] if d["name"] == "1 - Ground Floor Main")
    sfp = next(p for p in sw["sfp_ports"] if p["port"] == 49)
    assert sfp["sfp"]["sfp_rxpower"] == "-18.50" and sfp["sfp"]["sfp_temperature"] == "45.1"
    # non-SFP ports are not listed
    assert all(p["port"] == 49 for p in sw["sfp_ports"])


async def test_site_health_subsystems():
    fake = FakeFleet()
    r = await call(make(fake), "site_health", console="Office-UDM", site="Default")
    subs = {s["subsystem"]: s for s in r["sites"][0]["subsystems"]}
    assert subs["wan"]["latency_ms"] == 3 and subs["wlan"]["status"] == "warning"


async def test_client_app_usage_dpi():
    fake = FakeFleet()
    r = await call(make(fake), "client_app_usage", console="Office-UDM", site="Default")
    assert r["count"] == 1
    c = r["clients"][0]
    assert c["mac"] == "11:22:33:44:55:66" and c["name"] == "laptop"
    assert c["apps"][0].get("name") == "Instagram" and c["apps"][0]["mb"] == 306.0


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

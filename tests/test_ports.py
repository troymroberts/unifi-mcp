import json

import httpx

from unifi_mcp.config import Settings
from unifi_mcp.server import create_server

from test_server import HOST_OWNED, NET_SITES, call

NETWORKS = [
    {"_id": "net-core", "name": "Core LAN", "purpose": "corporate", "ip_subnet": "192.168.1.1/24"},
    {"_id": "net-voip", "name": "VOIP  LAN", "purpose": "corporate", "vlan": 5, "ip_subnet": "192.168.5.1/24"},
    {"_id": "net-cam", "name": "Camera LAN", "purpose": "corporate", "vlan": 4, "ip_subnet": "10.1.0.1/22"},
    {"_id": "net-wan", "name": "Internet 1", "purpose": "wan"},
]
PROFILES = [{"_id": "pc-voice", "name": "Voice phones"}]
SWITCH_MAC = "68:d7:9a:52:b9:67"


class FakeConsole:
    def __init__(self):
        self.switch = {
            "_id": "dev-sw3", "mac": SWITCH_MAC, "name": "4 - Old Switch 3", "model": "USL24P",
            "uplink": {"port_idx": 1},
            "port_table": [
                {"port_idx": 1, "up": True, "speed": 1000, "is_uplink": True},
                {"port_idx": 2, "up": True, "speed": 1000, "poe_mode": "auto", "poe_power": "3.1",
                 "native_networkconf_id": "net-core"},
                {"port_idx": 3, "up": True, "speed": 1000},
                {"port_idx": 4, "up": False},
            ],
            "port_overrides": [{"port_idx": 4, "name": "spare", "poe_mode": "off"},
                               {"port_idx": 2, "name": "old name", "native_networkconf_id": "net-core"}],
        }
        self.ap = {"_id": "dev-ap", "mac": "aa:aa:aa:00:00:01", "name": "Tent AP", "model": "U6",
                   "uplink": {"uplink_mac": SWITCH_MAC, "uplink_remote_port": 3}}
        self.sta = [{"mac": "C0:74:AD:6D:44:57", "ip": "192.168.1.162", "hostname": "GRP2634",
                     "sw_mac": SWITCH_MAC, "sw_port": 2},
                    {"mac": "11:22:33:44:55:66", "ip": "192.168.1.50", "name": "Till PC",
                     "sw_mac": SWITCH_MAC, "sw_port": 2}]
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ok = lambda data: httpx.Response(200, json={"meta": {"rc": "ok"}, "data": data})  # noqa: E731
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED]})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES)
        if path.endswith("/s/default/stat/device"):
            return ok([self.switch, self.ap])
        if path.endswith("/s/default/rest/networkconf"):
            return ok(NETWORKS)
        if path.endswith("/s/default/rest/portconf"):
            return ok(PROFILES)
        if path.endswith("/s/default/stat/sta"):
            return ok(self.sta)
        if path.endswith("/s/default/rest/device/dev-sw3") and request.method == "PUT":
            body = json.loads(request.content)
            self.puts.append((path, body))
            return ok([body])
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


def make(fake, writes=True):
    return create_server(Settings(sm_api_key="k", allow_writes=writes), transport=httpx.MockTransport(fake))


async def test_list_switch_ports_shows_vlans_clients_uplink_and_downstream():
    r = await call(make(FakeConsole()), "list_switch_ports", device="old switch 3", console="Office-UDM", site="Default")
    assert r["device"] == "4 - Old Switch 3"
    ports = {p["port"]: p for p in r["ports"]}
    assert ports[1]["uplink"] is True
    assert ports[2]["native_network"] == "Core LAN (vlan 1)" and ports[2]["name"] == "old name"
    assert [c["mac"] for c in ports[2]["clients"]] == ["c0:74:ad:6d:44:57", "11:22:33:44:55:66"]
    assert ports[3]["downstream_devices"] == ["Tent AP"]
    assert ports[4]["poe_mode"] == "off" and ports[4]["up"] is False
    up = await call(make(FakeConsole()), "list_switch_ports", device=SWITCH_MAC, console="Office-UDM",
                    site="Default", only_up=True)
    assert [p["port"] for p in up["ports"]] == [1, 2, 3]


async def test_native_vlan_change_dry_run_then_apply_keeps_other_overrides():
    fake = FakeConsole()
    args = dict(device="4 - Old Switch 3", console="Office-UDM", site="Default",
                ports=[{"port": 2, "native_network": 5, "name": "1576 Christmas Shop"}])
    plan = await call(make(fake), "set_switch_ports", **args)
    assert plan["dry_run"] is True and plan["changes"] == 1 and fake.puts == []
    p = plan["plan"][0]
    assert p["before"]["native_network"] == "Core LAN (vlan 1)"
    assert p["after"]["native_network"] == "VOIP  LAN (vlan 5)"
    assert len(p["clients_affected"]) == 2

    done = await call(make(fake), "set_switch_ports", confirm=True, **args)
    assert done["executed"] is True
    body = fake.puts[0][1]["port_overrides"]
    by_idx = {o["port_idx"]: o for o in body}
    assert by_idx[4] == {"port_idx": 4, "name": "spare", "poe_mode": "off"}  # untouched
    assert by_idx[2]["native_networkconf_id"] == "net-voip"
    assert "setting_preference" not in by_idx[2] and by_idx[2]["name"] == "1576 Christmas Shop"


async def test_refuses_uplink_and_device_port_and_writes_nothing():
    fake = FakeConsole()
    r = await call(make(fake), "set_switch_ports", device="4 - Old Switch 3", console="Office-UDM", site="Default",
                   confirm=True, ports=[{"port": 1, "native_network": "VOIP  LAN"},
                                        {"port": 3, "poe_mode": "off"},
                                        {"port": 2, "name": "ok on its own"}])
    assert r["dry_run"] is True and fake.puts == []
    assert any("uplink" in e for e in r["errors"]) and any("Tent AP" in e for e in r["errors"])


async def test_tagged_list_profile_exclusivity_and_bad_values():
    fake = FakeConsole()
    r = await call(make(fake), "set_switch_ports", device="4 - Old Switch 3", console="Office-UDM", site="Default",
                   confirm=True, ports=[{"port": 2, "native_network": "Core LAN", "tagged": ["VOIP  LAN"]}])
    o = {x["port_idx"]: x for x in fake.puts[0][1]["port_overrides"]}[2]
    assert o["tagged_vlan_mgmt"] == "custom" and o["excluded_networkconf_ids"] == ["net-cam"]  # not WAN, not native
    bad = await call(make(FakeConsole()), "set_switch_ports", device="4 - Old Switch 3", console="Office-UDM",
                     site="Default", ports=[{"port": 2, "profile": "Voice phones", "native_network": 5},
                                            {"port": 4, "poe_mode": "pasv24"},
                                            {"port": 9, "name": "x"}])
    assert len(bad["errors"]) == 3


async def test_profile_switches_back_to_auto_and_drops_manual_vlans():
    fake = FakeConsole()
    await call(make(fake), "set_switch_ports", device="dev-sw3", console="Office-UDM", site="Default", confirm=True,
               ports=[{"port": 2, "profile": "Voice phones"}])
    o = {x["port_idx"]: x for x in fake.puts[0][1]["port_overrides"]}[2]
    assert o["portconf_id"] == "pc-voice" and "setting_preference" not in o
    assert "native_networkconf_id" not in o


async def test_writes_disabled_returns_plan_only():
    fake = FakeConsole()
    r = await call(make(fake, writes=False), "set_switch_ports", device="4 - Old Switch 3", console="Office-UDM",
                   site="Default", confirm=True, ports=[{"port": 2, "native_network": 5}])
    assert r["dry_run"] is True and "UNIFI_ALLOW_WRITES" in r["next"] and fake.puts == []

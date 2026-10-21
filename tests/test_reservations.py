import json

import httpx

from unifi_mcp.config import Settings
from unifi_mcp.server import create_server

from test_server import HOST_OWNED, NET_SITES, call

NETWORKS = {"meta": {"rc": "ok"}, "data": [
    {"_id": "net-lan", "name": "Default", "purpose": "corporate", "ip_subnet": "192.168.1.1/24"},
    {"_id": "net-cam", "name": "Camera LAN", "purpose": "corporate", "vlan": 4, "ip_subnet": "10.1.0.1/22",
     "dhcpd_start": "10.1.0.50", "dhcpd_stop": "10.1.3.254"},
]}


class FakeConsole:
    def __init__(self):
        self.users = [
            {"_id": "u1", "mac": "E4:24:6C:4C:50:D9", "name": "camera153", "hostname": "7F01267RAG1EF9E"},
            {"_id": "u2", "mac": "9c:14:63:26:72:33", "name": "Dahua IVSS", "use_fixedip": True,
             "fixed_ip": "10.1.0.16", "network_id": "net-cam"},
        ]
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED]})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES)
        if path.endswith("/s/default/rest/networkconf"):
            return httpx.Response(200, json=NETWORKS)
        if path.endswith("/s/default/rest/user") and request.method == "GET":
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": self.users})
        if "/s/default/rest/user" in path and request.method in ("PUT", "POST"):
            body = json.loads(request.content)
            self.writes.append((request.method, path.rsplit("/rest/", 1)[1], body))
            return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [body]})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


def make(fake, writes=True):
    return create_server(Settings(sm_api_key="k", allow_writes=writes), transport=httpx.MockTransport(fake))


async def test_list_fixed_ips_reads_classic_users_and_networks():
    r = await call(make(FakeConsole()), "list_fixed_ips", console="Office-UDM", site="Default")
    assert r["count"] == 1
    assert r["clients"][0] == {"mac": "9c:14:63:26:72:33", "name": "Dahua IVSS", "hostname": None,
                               "fixed_ip": "10.1.0.16", "network": "Camera LAN"}
    both = await call(make(FakeConsole()), "list_fixed_ips", console="Office-UDM", site="Default", include_names=True)
    assert both["count"] == 2


async def test_plan_then_apply_reservation_and_name():
    fake = FakeConsole()
    items = [{"mac": "e4-24-6c-4c-50-d9", "ip": "10.1.1.20", "name": "Lane 3 Cam"},
             {"mac": "9c:14:63:26:72:33", "ip": "10.1.0.16"},              # already so: no change
             {"mac": "aa:bb:cc:dd:ee:01", "ip": "10.1.1.21", "name": "New"}]  # unknown client: create
    dry = await call(make(fake), "set_client_reservations", items=items, console="Office-UDM", site="Default")
    assert dry["dry_run"] is True and dry["changes"] == 2 and dry["unchanged"] == 1 and not fake.writes
    assert dry["plan"][0]["before"]["fixed_ip"] is None and dry["plan"][0]["after"] == {"name": "Lane 3 Cam", "fixed_ip": "10.1.1.20"}
    assert dry["plan"][0]["network"] == "Camera LAN"

    done = await call(make(fake), "set_client_reservations", items=items, console="Office-UDM", site="Default", confirm=True)
    assert done["executed"] is True and done["applied"] == 2 and done["failed"] == 0
    assert fake.writes[0] == ("PUT", "user/u1", {"use_fixedip": True, "fixed_ip": "10.1.1.20", "network_id": "net-cam", "name": "Lane 3 Cam"})
    assert fake.writes[1] == ("POST", "user", {"mac": "aa:bb:cc:dd:ee:01", "use_fixedip": True, "fixed_ip": "10.1.1.21",
                                               "network_id": "net-cam", "name": "New"})


async def test_validation_blocks_the_whole_batch():
    fake = FakeConsole()
    items = [{"mac": "e4:24:6c:4c:50:d9", "ip": "10.1.0.16"},   # reserved for the IVSS
             {"mac": "aa:bb:cc:dd:ee:02", "ip": "10.1.0.1"},    # gateway
             {"mac": "aa:bb:cc:dd:ee:03", "ip": "10.9.9.9"},    # in no network
             {"mac": "aa:bb:cc:dd:ee:04", "ip": "10.1.2.2"},
             {"mac": "aa:bb:cc:dd:ee:05", "ip": "10.1.2.2"},    # duplicate in batch
             {"mac": "bogus", "ip": "10.1.2.3"}]
    r = await call(make(fake), "set_client_reservations", items=items, console="Office-UDM", site="Default", confirm=True)
    assert r["dry_run"] is True and not fake.writes
    text = " | ".join(r["errors"])
    for needle in ("already reserved for 9c:14:63:26:72:33", "gateway", "no single network contains 10.9.9.9",
                   "10.1.2.2 also given to aa:bb:cc:dd:ee:04", "not a MAC"):
        assert needle in text, needle


async def test_name_only_and_writes_disabled():
    fake = FakeConsole()
    r = await call(make(fake, writes=False), "set_client_reservations",
                   items=[{"mac": "e4:24:6c:4c:50:d9", "name": "Carpark 1"}], console="Office-UDM", site="Default", confirm=True)
    assert r["dry_run"] is True and "UNIFI_ALLOW_WRITES" in r["next"] and not fake.writes
    ok = await call(make(fake), "set_client_reservations",
                    items=[{"mac": "e4:24:6c:4c:50:d9", "name": "Carpark 1"}], console="Office-UDM", site="Default", confirm=True)
    assert fake.writes == [("PUT", "user/u1", {"name": "Carpark 1"})] and ok["applied"] == 1

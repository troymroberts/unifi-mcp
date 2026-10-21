import json
import textwrap

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unifi_mcp.config import Settings, load_settings
from unifi_mcp.server import create_server

HOST_OWNED = {
    "id": "AAA:1",
    "type": "console",
    "owner": True,
    "ipAddress": "1.2.3.4",
    "reportedState": {"hostname": "Office-UDM", "state": "connected", "version": "5.1.132",
                      "hardware": {"shortname": "UDMPRO"}},
}
HOST_SHARED = {**HOST_OWNED, "id": "BBB:2", "owner": False,
               "reportedState": {**HOST_OWNED["reportedState"], "hostname": "Partner-UDM"}}
HOST_OFFLINE = {**HOST_OWNED, "id": "CCC:3",
                "reportedState": {**HOST_OWNED["reportedState"], "hostname": "Old-Cloudkey", "state": "disconnected"}}
CLOUD_SITES = [
    {"siteId": "s1", "hostId": "AAA:1", "meta": {"desc": "Default"},
     "statistics": {"counts": {"totalDevice": 4, "offlineDevice": 1, "wifiClient": 3, "wiredClient": 2}}},
]
NET_SITES = {"offset": 0, "limit": 25, "count": 2, "totalCount": 2,
             "data": [{"id": "uuid-default", "internalReference": "default", "name": "Default"},
                      {"id": "uuid-shop", "internalReference": "x9", "name": "Shop"}]}


class FakeUniFi:
    """Records requests and answers like api.ui.com + the Connector."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED, HOST_SHARED, HOST_OFFLINE]})
        if path == "/v1/sites":
            return httpx.Response(200, json={"data": CLOUD_SITES})
        if path == "/v1/devices":
            return httpx.Response(200, json={"data": [{"hostId": "AAA:1", "hostName": "Office-UDM", "devices": [
                {"name": "AP", "status": "online", "productLine": "network", "updateAvailable": "6.8"}]}]})
        if path.startswith("/v1/connector/consoles/BBB:2/"):
            return httpx.Response(403, json={"code": "forbidden", "message": "user is not the owner of this host"})
        if path.endswith("/network/integration/v1/sites"):
            return httpx.Response(200, json=NET_SITES)
        if path.endswith("/devices") and request.method == "GET":
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            data = [{"id": f"d{i}"} for i in range(offset, min(offset + limit, 250))]
            return httpx.Response(200, json={"offset": offset, "limit": limit, "count": len(data),
                                             "totalCount": 250, "data": data})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "echo": json.loads(request.content)})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})


@pytest.fixture
def fake():
    return FakeUniFi()


def server(fake, **overrides):
    settings = Settings(sm_api_key="sm-key", **overrides)
    return create_server(settings, transport=httpx.MockTransport(fake))


async def call(srv, name, **args):
    result = await srv.call_tool(name, args)
    content = result.structured_content or {}
    # Non-dict return values are wrapped by the SDK as {"result": ...}.
    return content["result"] if set(content) == {"result"} else content


async def test_list_consoles_marks_connector_access(fake):
    rows = await call(server(fake), "list_consoles", include_disconnected=True)
    access = {r["name"]: r["connector_access"] for r in rows}
    assert access["Office-UDM"] == "yes"
    assert "organization key" in access["Partner-UDM"]
    assert access["Old-Cloudkey"].startswith("no")
    assert fake.requests[0].headers["X-API-Key"] == "sm-key"


async def test_console_resolution_by_partial_name_and_site_name(fake):
    page = await call(server(fake), "list_devices", console="office", site="shop", limit=10)
    assert page["count"] == 10
    last = fake.requests[-1]
    assert last.url.path == "/v1/connector/consoles/AAA:1/network/integration/v1/sites/uuid-shop/devices"


async def test_pagination_follows_offset(fake):
    page = await call(server(fake), "list_devices", console="Office-UDM", site="default", limit=1000)
    assert page["count"] == 250 and page["totalCount"] == 250
    assert [r.url.params["offset"] for r in fake.requests if r.url.path.endswith("/devices")] == ["0", "200"]


async def test_multi_site_console_requires_site(fake):
    with pytest.raises(ToolError, match="pass site="):
        await call(server(fake), "list_devices", console="Office-UDM")


async def test_disconnected_console_is_rejected(fake):
    with pytest.raises(ToolError, match="disconnected"):
        await call(server(fake), "list_sites", console="Old-Cloudkey")


async def test_403_explains_org_key(fake):
    with pytest.raises(ToolError, match="organization key"):
        await call(server(fake), "list_sites", console="Partner")


async def test_writes_hidden_and_blocked_by_default(fake):
    srv = server(fake)
    names = {t.name for t in await srv.list_tools()}
    assert "restart_device" not in names
    with pytest.raises(ToolError, match="writes are disabled"):
        await call(srv, "network_api_request", method="POST", path="/v1/sites/{siteId}/networks",
                   console="Office-UDM", site="default", body={})
    assert not [r for r in fake.requests if r.method == "POST"]


async def test_write_dry_run_then_confirm(fake):
    srv = server(fake, allow_writes=True)
    preview = await call(srv, "restart_device", device_id="dev1", console="Office-UDM", site="default")
    assert preview["dry_run"] and preview["body"] == {"action": "RESTART"}
    assert not [r for r in fake.requests if r.method == "POST"]
    done = await call(srv, "restart_device", device_id="dev1", console="Office-UDM", site="default", confirm=True)
    assert done["executed"]
    post = [r for r in fake.requests if r.method == "POST"][0]
    assert post.url.path.endswith("/sites/uuid-default/devices/dev1/actions")


async def test_generic_request_fills_site_and_rejects_unfilled_params(fake):
    srv = server(fake)
    with pytest.raises(ToolError, match="networkId"):
        await call(srv, "network_api_request", method="GET", path="/v1/sites/{siteId}/networks/{networkId}",
                   console="Office-UDM", site="default")


async def test_fleet_health(fake):
    health = await call(server(fake), "fleet_health")
    assert health["consoles_total"] == 3 and health["consoles_connected"] == 2
    assert health["no_connector_access"] == ["Partner-UDM"]
    assert health["sites_with_problems"][0]["offline_devices"] == 1
    assert health["devices_needing_update"][0]["name"] == "AP"


async def test_local_alias_goes_direct(fake, tmp_path, monkeypatch):
    cfg = tmp_path / "consoles.yaml"
    cfg.write_text(textwrap.dedent("""
        default: home
        consoles:
          home:
            local_url: https://192.168.1.1/
            api_key_env: HOME_KEY
            verify_ssl: false
          shop:
            host: Office-UDM
    """))
    monkeypatch.setenv("HOME_KEY", "local-key")
    settings = load_settings({"UNIFI_CONSOLES_FILE": str(cfg), "UNIFI_SM_API_KEY": "sm-key"})
    assert settings.default_console == "home"
    srv = create_server(settings, transport=httpx.MockTransport(fake))
    await call(srv, "list_sites")
    req = fake.requests[-1]
    assert str(req.url) == "https://192.168.1.1/proxy/network/integration/v1/sites?offset=0&limit=200"
    assert req.headers["X-API-Key"] == "local-key"
    await call(srv, "list_sites", console="shop")
    assert fake.requests[-1].url.path.startswith("/v1/connector/consoles/AAA:1/")


def test_spec_search_and_describe():
    from unifi_mcp import spec

    assert any(op["path"] == "/v1/sites/{siteId}/firewall/policies" for op in spec.search("firewall policies"))
    described = spec.describe("POST", "/v1/sites/{siteId}/hotspot/vouchers")
    assert "timeLimitMinutes" in described["requestBody"]["required"]


def test_secrets_file_fills_env_without_overriding(tmp_path):
    from unifi_mcp.config import load_secrets_file

    secrets = tmp_path / "secrets.env"
    secrets.write_text("# keys\nUNIFI_SM_API_KEY='from-file'\nexport UNIFI_HOME_API_KEY=local\nUNIFI_TIMEOUT=10\n")
    secrets.chmod(0o644)
    env = {"UNIFI_TIMEOUT": "60"}
    warnings = load_secrets_file(secrets, env)
    assert env == {"UNIFI_TIMEOUT": "60", "UNIFI_SM_API_KEY": "from-file", "UNIFI_HOME_API_KEY": "local"}
    assert warnings and "chmod 600" in warnings[0]

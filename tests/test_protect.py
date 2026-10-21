import httpx
import pytest

from unifi_mcp.config import Settings
from unifi_mcp.protect import plates_from_event
from unifi_mcp.server import create_server
from test_server import HOST_OWNED, call

CAMERAS = [
    {"id": "cam-lpr", "name": "Gate LPR", "type": "UVC AI LPR", "state": "CONNECTED",
     "featureFlags": {"smartDetectTypes": ["vehicle", "licensePlate"]},
     "smartDetectSettings": {"objectTypes": ["vehicle", "licensePlate"]}},
    {"id": "cam-door", "name": "Front Door", "type": "UVC G4 Pro", "state": "CONNECTED",
     "featureFlags": {"smartDetectTypes": ["person"]}, "smartDetectSettings": {"objectTypes": ["person"]}},
]
EVENTS = [
    {"id": "e2", "type": "smartDetectZone", "start": 1792600000000, "end": 1792600005000, "camera": "cam-lpr",
     "smartDetectTypes": ["vehicle", "licensePlate"],
     "metadata": {"detectedThumbnails": [
         {"type": "vehicle", "name": "PCX1234", "confidence": 91,
          "attributes": {"color": {"val": "white"}, "vehicleType": {"val": "suv"}}},
         {"type": "person"}]}},
    {"id": "e1", "type": "smartDetectZone", "start": 1792590000000, "end": 1792590004000, "camera": "cam-lpr",
     "smartDetectTypes": ["licensePlate"], "metadata": {"licensePlate": {"name": "PCX1234", "confidenceLevel": 88}}},
    {"id": "e0", "type": "smartDetectZone", "start": 1792580000000, "end": 1792580004000, "camera": "cam-lpr",
     "smartDetectTypes": ["licensePlate"], "metadata": {}},
]


class FakeProtect:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/hosts":
            return httpx.Response(200, json={"data": [HOST_OWNED]})
        if path.endswith("/protect/integration/v1/cameras"):
            return httpx.Response(200, json=CAMERAS)
        if path.endswith("/protect/integration/v1/cameras/cam-door/snapshot"):
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        if path.endswith("/protect/api/nvr"):
            return httpx.Response(200, json={"timezone": "America/Port_of_Spain"})
        if path.endswith("/protect/api/events"):
            return httpx.Response(200, json=EVENTS)
        if request.method != "GET":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"message": f"unexpected {path}"})


@pytest.fixture
def fake():
    return FakeProtect()


def server(fake, **kw):
    return create_server(Settings(sm_api_key="sm-key", **kw), transport=httpx.MockTransport(fake))


def test_plates_from_event_merges_sources():
    plates = plates_from_event(EVENTS[0])
    assert plates == [{"plate": "PCX1234", "confidence": 91, "vehicle_type": "suv", "color": "white"}]
    assert plates_from_event(EVENTS[1]) == [{"plate": "PCX1234", "confidence": 88}]
    assert plates_from_event(EVENTS[2]) == []


async def test_list_cameras_flags_lpr(fake):
    rows = await call(server(fake), "protect_list_cameras", console="Office-UDM")
    assert {r["name"]: r["license_plates"] for r in rows} == {"Gate LPR": "enabled", "Front Door": "no"}
    assert fake.requests[-1].url.path == "/v1/connector/consoles/AAA:1/protect/integration/v1/cameras"


async def test_license_plates_today_in_nvr_timezone(fake):
    result = await call(server(fake), "protect_license_plates", console="Office-UDM", camera="gate")
    assert result["timezone"] == "America/Port_of_Spain" and result["from"].endswith("T00:00:00-04:00")
    assert result["unique_plates"] == 1 and result["plates"][0]["sightings"] == 2
    assert result["events_without_plate_text"] == 1 and result["lpr_cameras"] == ["Gate LPR"]
    events_req = [r for r in fake.requests if r.url.path.endswith("/protect/api/events")][0]
    assert events_req.url.params.get_list("smartDetectTypes") == ["licensePlate"]
    assert events_req.url.params.get_list("cameras") == ["cam-lpr"]


async def test_events_window_by_hours(fake):
    result = await call(server(fake), "protect_events", console="Office-UDM", hours=2, smart_types=["person"])
    assert result["count"] == 3 and result["events"][0]["camera"] == "Gate LPR"
    req = [r for r in fake.requests if r.url.path.endswith("/protect/api/events")][0]
    assert int(req.url.params["end"]) - int(req.url.params["start"]) == pytest.approx(2 * 3600 * 1000, abs=1000)


async def test_snapshot_returns_image(fake):
    result = await server(fake).call_tool("protect_camera_snapshot", {"camera": "Front Door", "console": "Office-UDM"})
    assert result.content[0].type == "image" and result.content[0].mime_type == "image/jpeg"


async def test_protect_writes_gated(fake):
    srv = server(fake)
    with pytest.raises(Exception, match="writes are disabled"):
        await call(srv, "protect_api_request", method="PATCH", path="/v1/cameras/cam-door", console="Office-UDM",
                   body={"name": "x"})
    preview = await call(server(fake, allow_writes=True), "protect_api_request", method="PATCH",
                         path="/v1/cameras/cam-door", console="Office-UDM", body={"name": "x"})
    assert preview["dry_run"] and preview["app"] == "protect/integration"
    assert not [r for r in fake.requests if r.method != "GET"]

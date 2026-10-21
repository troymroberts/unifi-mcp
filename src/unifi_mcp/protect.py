"""UniFi Protect tools.

Devices, snapshots and generic calls use the official Protect Integration API
(``/protect/integration/v1``). Event history is not part of that API, so events and
license plates read Protect's internal API (``/protect/api``), which the cloud Connector
also forwards with the Site Manager key. The internal API is undocumented and may change.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import PROTECT_APP, PROTECT_INTERNAL_APP, NetworkAPI, NetworkTarget
from .config import Settings
from .resolve import Resolver, ResolveError, _match

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
CACHE_TTL = 300
MAX_EVENTS = 2000

PROTECT_DEVICE_PATHS: dict[str, str] = {
    "nvrs": "/v1/nvrs",
    "viewers": "/v1/viewers",
    "liveviews": "/v1/liveviews",
    "lights": "/v1/lights",
    "sensors": "/v1/sensors",
    "sirens": "/v1/sirens",
    "speakers": "/v1/speakers",
    "chimes": "/v1/chimes",
    "relays": "/v1/relays",
    "bridges": "/v1/bridges",
    "link-stations": "/v1/link-stations",
    "alarm-hubs": "/v1/alarm-hubs",
    "fobs": "/v1/fobs",
    "arm-profiles": "/v1/arm-profiles",
    "users": "/v1/users",
}
ProtectDevice = Literal[
    "nvrs", "viewers", "liveviews", "lights", "sensors", "sirens", "speakers", "chimes", "relays",
    "bridges", "link-stations", "alarm-hubs", "fobs", "arm-profiles", "users",
]
SmartType = Literal["person", "vehicle", "package", "licensePlate", "face", "animal"]


def summarize_camera(cam: dict[str, Any]) -> dict[str, Any]:
    supported = (cam.get("featureFlags") or {}).get("smartDetectTypes") or []
    enabled = (cam.get("smartDetectSettings") or {}).get("objectTypes") or []
    return {
        "id": cam.get("id"),
        "name": cam.get("name"),
        "model": cam.get("type") or cam.get("modelKey"),
        "state": cam.get("state"),
        "mac": cam.get("mac"),
        "smart_detect_supported": supported,
        "smart_detect_enabled": enabled,
        "license_plates": "enabled" if "licensePlate" in enabled
        else ("supported, off" if "licensePlate" in supported else "no"),
    }


def plates_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Plate readings in an event. Protect has stored them in a few places across versions."""
    meta = event.get("metadata") or {}
    found: dict[str, dict[str, Any]] = {}
    plate = meta.get("licensePlate")
    if isinstance(plate, dict) and plate.get("name"):
        found[plate["name"]] = {"plate": plate["name"], "confidence": plate.get("confidenceLevel")}
    for thumb in meta.get("detectedThumbnails") or []:
        name = thumb.get("name")
        if not name or thumb.get("type") not in ("vehicle", "licensePlate"):
            continue
        attrs = thumb.get("attributes") or {}
        entry = found.setdefault(name, {"plate": name})
        entry.setdefault("confidence", thumb.get("confidence"))
        for key, attr in (("vehicle_type", "vehicleType"), ("color", "color")):
            value = attrs.get(attr)
            if isinstance(value, dict):
                value = value.get("val")
            if value:
                entry[key] = value
    return list(found.values())


def register(
    mcp: MCPServer,
    settings: Settings,
    resolver: Resolver,
    api: NetworkAPI,
    execute_write: Callable[..., Awaitable[dict[str, Any]]],
    guard: Callable,
) -> None:
    cameras_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    tz_cache: dict[str, ZoneInfo] = {}

    async def cameras(target: NetworkTarget, refresh: bool = False) -> list[dict[str, Any]]:
        cached = cameras_cache.get(target.root)
        if refresh or not cached or time.monotonic() - cached[0] > CACHE_TTL:
            cached = (time.monotonic(), await api.request(target, "GET", "/v1/cameras", app=PROTECT_APP))
            cameras_cache[target.root] = cached
        return cached[1]

    async def camera_id(target: NetworkTarget, query: str) -> str:
        cams = await cameras(target)
        candidates = [(c["id"], c) for c in cams] + [(c.get("name") or "", c) for c in cams]
        return _match(query, candidates, "camera")["id"]

    async def timezone(target: NetworkTarget, override: str | None) -> ZoneInfo:
        name = override
        if not name:
            if target.root not in tz_cache:
                try:
                    nvr = await api.request(target, "GET", "/nvr", app=PROTECT_INTERNAL_APP)
                    tz_cache[target.root] = ZoneInfo(nvr.get("timezone") or "UTC")
                except Exception:
                    tz_cache[target.root] = ZoneInfo("UTC")
            return tz_cache[target.root]
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone '{name}'") from exc

    def window(tz: ZoneInfo, date: str | None, hours: float | None) -> tuple[datetime, datetime]:
        now = datetime.now(tz)
        if hours:
            return now - timedelta(hours=hours), now
        if not date or date == "today":
            day = now.date()
        elif date == "yesterday":
            day = now.date() - timedelta(days=1)
        else:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        return start, min(start + timedelta(days=1), now)

    async def fetch_events(
        target: NetworkTarget, start: datetime, end: datetime, *, smart_types: list[str] | None,
        camera: str | None, event_types: list[str] | None, limit: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
            "limit": min(limit, MAX_EVENTS),
            "orderDirection": "DESC",
        }
        if event_types:
            params["types"] = event_types
        if smart_types:
            params["smartDetectTypes"] = smart_types
        if camera:
            params["cameras"] = [await camera_id(target, camera)]
        events = await api.request(target, "GET", "/events", params=params, app=PROTECT_INTERNAL_APP)
        if not isinstance(events, list):
            raise ToolError(f"unexpected events response from Protect: {str(events)[:200]}")
        return events

    def local_time(ms: int | None, tz: ZoneInfo) -> str | None:
        return datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="seconds") if ms else None

    @mcp.tool(annotations=READ)
    @guard
    async def protect_list_cameras(console: str | None = None, refresh: bool = False) -> list[dict[str, Any]]:
        """Protect cameras on a console: name, model, state and which smart detections (person, vehicle,
        licensePlate, face, ...) are supported and enabled."""
        target = await resolver.console(console)
        return [summarize_camera(c) for c in await cameras(target, refresh)]

    @mcp.tool(annotations=READ)
    @guard
    async def protect_list_devices(kind: ProtectDevice, console: str | None = None) -> Any:
        """Other Protect objects: nvrs, viewers, liveviews, lights, sensors, sirens, speakers, chimes,
        relays, bridges, link-stations, alarm-hubs, fobs, arm-profiles, users."""
        target = await resolver.console(console)
        return await api.request(target, "GET", PROTECT_DEVICE_PATHS[kind], app=PROTECT_APP)

    @mcp.tool(annotations=READ)
    @guard
    async def protect_camera_snapshot(camera: str, console: str | None = None, high_quality: bool = False) -> Image:
        """Current still image from a camera (name or id)."""
        target = await resolver.console(console)
        cam = await camera_id(target, camera)
        data, content_type = await api.request(
            target, "GET", f"/v1/cameras/{cam}/snapshot", params={"highQuality": str(high_quality).lower()},
            app=PROTECT_APP, raw=True,
        )
        return Image(data=data, format=(content_type.split("/")[-1].split(";")[0] or "jpeg"))

    @mcp.tool(annotations=READ)
    @guard
    async def protect_events(
        console: str | None = None,
        date: str | None = None,
        hours: float | None = None,
        smart_types: list[SmartType] | None = None,
        camera: str | None = None,
        event_types: list[str] | None = None,
        limit: int = 200,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        """Protect event history (motion, smart detections, rings, ...), newest first.
        Window: date ('today' default, 'yesterday' or YYYY-MM-DD in the NVR's timezone) or the last
        `hours`. Filter by smart_types (e.g. ['person']), camera (name or id) and event_types
        (e.g. ['smartDetectZone', 'smartDetectLine', 'motion', 'ring'])."""
        target = await resolver.console(console)
        tz = await timezone(target, timezone_name)
        start, end = window(tz, date, hours)
        events = await fetch_events(target, start, end, smart_types=list(smart_types or []) or None,
                                    camera=camera, event_types=event_types, limit=limit)
        names = {c["id"]: c.get("name") for c in await cameras(target)}
        rows = []
        for e in events:
            row = {
                "time": local_time(e.get("start"), tz),
                "end": local_time(e.get("end"), tz),
                "camera": names.get(e.get("camera"), e.get("camera")),
                "type": e.get("type"),
                "smart_types": e.get("smartDetectTypes"),
                "score": e.get("score"),
                "id": e.get("id"),
            }
            if plates := plates_from_event(e):
                row["plates"] = plates
            rows.append(row)
        return {"console": target.label, "timezone": str(tz), "from": start.isoformat(timespec="seconds"),
                "to": end.isoformat(timespec="seconds"), "count": len(rows), "truncated": len(events) >= limit,
                "events": rows}

    @mcp.tool(annotations=READ)
    @guard
    async def protect_license_plates(
        console: str | None = None,
        date: str | None = None,
        hours: float | None = None,
        camera: str | None = None,
        limit: int = 1000,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        """License plates read by Protect LPR cameras, newest first, plus a per-plate summary.
        Window: date ('today' default, 'yesterday' or YYYY-MM-DD in the NVR's timezone) or the last `hours`."""
        target = await resolver.console(console)
        tz = await timezone(target, timezone_name)
        start, end = window(tz, date, hours)
        events = await fetch_events(target, start, end, smart_types=["licensePlate"], camera=camera,
                                    event_types=["smartDetectZone", "smartDetectLine"], limit=limit)
        names = {c["id"]: c.get("name") for c in await cameras(target)}
        detections, summary = [], {}
        for e in events:
            for p in plates_from_event(e):
                when = local_time(e.get("start"), tz)
                detections.append({"time": when, "camera": names.get(e.get("camera"), e.get("camera")),
                                   **p, "event_id": e.get("id")})
                s = summary.setdefault(p["plate"], {"plate": p["plate"], "sightings": 0, "first": when, "last": when})
                s["sightings"] += 1
                s["first"] = min(s["first"], when)
                s["last"] = max(s["last"], when)
        lpr = [c.get("name") for c in await cameras(target)
               if "licensePlate" in ((c.get("smartDetectSettings") or {}).get("objectTypes") or [])]
        return {
            "console": target.label, "timezone": str(tz),
            "from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"),
            "lpr_cameras": lpr,
            "plate_events": len(events),
            "events_without_plate_text": sum(1 for e in events if not plates_from_event(e)),
            "unique_plates": len(summary),
            "plates": sorted(summary.values(), key=lambda s: s["last"], reverse=True),
            "detections": detections,
            "truncated": len(events) >= limit,
        }

    @mcp.tool(annotations=WRITE)
    @guard
    async def protect_api_request(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        console: str | None = None,
        api_kind: Literal["integration", "internal"] = "integration",
        query: dict[str, Any] | None = None,
        body: Any = None,
        confirm: bool = False,
    ) -> Any:
        """Call any Protect path. api_kind='integration' is the official API (developer.ui.com/protect,
        e.g. GET /v1/cameras); 'internal' is Protect's own API (e.g. GET /nvr, /events), GET only.
        Non-GET integration calls need UNIFI_ALLOW_WRITES=true and return a dry-run until confirm=true."""
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        target = await resolver.console(console)
        app = PROTECT_APP if api_kind == "integration" else PROTECT_INTERNAL_APP
        if method == "GET":
            return await api.request(target, "GET", path, params=query, app=app)
        if api_kind == "internal":
            raise ResolveError("the internal Protect API is read-only here; use api_kind='integration'")
        return await execute_write(target, method, path, body, confirm, query, app=app)

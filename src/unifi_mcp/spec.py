"""Search the bundled Network Integration OpenAPI spec (backs the generic request tool)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

NETWORK_SPEC = "network-10.6.106.json"
METHODS = ("get", "post", "put", "patch", "delete")
MAX_REF_DEPTH = 6


@lru_cache(maxsize=None)
def load(name: str = NETWORK_SPEC) -> dict[str, Any]:
    return json.loads(resources.files("unifi_mcp.specs").joinpath(name).read_text())


def operations(spec: dict[str, Any] | None = None) -> list[dict[str, str]]:
    spec = spec or load()
    ops = []
    for path, item in spec["paths"].items():
        for method in METHODS:
            if method in item:
                ops.append({"method": method.upper(), "path": path, "summary": item[method].get("summary", "")})
    return ops


def search(query: str | None = None, method: str | None = None) -> list[dict[str, str]]:
    terms = (query or "").lower().split()
    return [
        op
        for op in operations()
        if (not method or op["method"] == method.upper())
        and all(t in f"{op['path']} {op['summary']}".lower() for t in terms)
    ]


def _resolve(node: Any, spec: dict[str, Any], depth: int = 0) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            if depth >= MAX_REF_DEPTH:
                return {"$ref": name}
            return _resolve(spec["components"]["schemas"][name], spec, depth + 1)
        return {k: _resolve(v, spec, depth) for k, v in node.items() if k not in ("example", "examples")}
    if isinstance(node, list):
        return [_resolve(v, spec, depth) for v in node]
    return node


def describe(method: str, path: str) -> dict[str, Any]:
    spec = load()
    item = spec["paths"].get(path)
    op = item.get(method.lower()) if item else None
    if not op:
        raise KeyError(f"{method.upper()} {path} is not in the Network API spec; use network_api_endpoints")
    body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
    return {
        "method": method.upper(),
        "path": path,
        "summary": op.get("summary"),
        "description": op.get("description"),
        "parameters": [
            {"name": p["name"], "in": p["in"], "required": p.get("required", False), "schema": p.get("schema")}
            for p in (op.get("parameters") or [])
        ],
        "requestBody": _resolve(body, spec) if body else None,
    }

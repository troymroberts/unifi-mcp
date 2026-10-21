# unifi-mcp

An MCP server for managing UniFi deployments from Claude (or any MCP client).

- **Fleet view** across every console on your UniFi account, through the
  [Site Manager API](https://developer.ui.com/site-manager/v1.0.0/) at `api.ui.com`.
- **Per-console control** through the official
  [Network Integration API](https://developer.ui.com/network/v10.6.106/), reached either through the
  Site Manager **Connector** (`/v1/connector/consoles/{id}/network/integration/...`, no VPN needed)
  or directly on a local console (`https://<console>/proxy/network/integration/...`).
- **Read-only by default.** Write tools only exist when `UNIFI_ALLOW_WRITES=true`, and every write
  first returns a dry-run preview; nothing changes until the call is repeated with `confirm=true`.

## Tools

| Tool | What it does |
|---|---|
| `list_consoles` | Consoles with version, state, sites, device counts and whether the Connector can reach them |
| `list_cloud_sites` | Every site with device/client counts, offline devices, WAN issues (`problems_only`) |
| `list_cloud_devices` | Every device in the fleet; filter by console, status, product line, `needs_update` |
| `fleet_health` | One-call summary: disconnected consoles, problem sites, devices needing updates |
| `get_isp_metrics` | Per-site ISP latency/packet loss/uptime over 24h, 7d or 30d (`raw` for every period) |
| `cloud_api_get` | Any read-only Site Manager path (e.g. `/v1/sd-wan-configs`) |
| `list_sites` | Network sites on one console |
| `list_devices`, `get_device` | Adopted devices; one device's details plus latest statistics |
| `list_pending_devices` | Devices waiting for adoption |
| `list_clients`, `get_client` | Connected clients |
| `list_site_config` | networks, wifi, firewall-policies/zones, acl-rules, dns-policies, wans, vpn-servers/tunnels, vouchers, radius-profiles, device-tags, LAGs, switch stacks, traffic-matching-lists |
| `network_api_endpoints`, `network_api_describe` | Search the bundled Network API spec; show parameters and request body schema |
| `network_api_request` | Call any Network API path (GET runs; other methods are gated writes) |
| `restart_device`, `power_cycle_port`, `set_guest_access`, `create_vouchers` | Curated writes (only with `UNIFI_ALLOW_WRITES=true`) |
| `protect_list_cameras` | Protect cameras with state and which smart detections (incl. license plates) are supported/enabled |
| `protect_list_devices` | NVRs, sensors, lights, chimes, viewers, liveviews, sirens, relays, alarm hubs, users, ... |
| `protect_camera_snapshot` | Current still image from a camera |
| `protect_events` | Event history (motion, smart detections, rings, ...) for a day or the last N hours, in the NVR's timezone |
| `protect_license_plates` | Plates read by LPR cameras for a day or window: per-plate sightings plus every detection |
| `protect_api_request` | Any official Protect API path (writes gated) or a read-only internal Protect path |

`console=` accepts a `consoles.yaml` alias, a console hostname (a unique partial match is fine) or a
Site Manager host id. `site=` accepts a site name, internal reference or id, and can be omitted when
the console has a single site. List filters use the Network API filter syntax, e.g.
`state.eq('OFFLINE')`.

## Install (Claude Code plugin — recommended)

In a Claude Code session:

```
/plugin install unifi --marketplace troymroberts/unifi-mcp
```

(or `/plugin marketplace add troymroberts/unifi-mcp` then `/plugin install unifi@unifi-mcp` on older
versions). Claude Code prompts for the settings below when the plugin is enabled; keys are masked and
kept in its secure credential store, not in plain-text config. Change them later under `/plugin` >
Installed > unifi > Configure options. Requires [uv](https://docs.astral.sh/uv/) on your PATH.

| Prompt | Notes |
|---|---|
| Site Manager API key | unifi.ui.com > API. A personal key reaches only the consoles you own; an **organization key** reaches every console in the organization. |
| Allow changes | Off by default. Exposes write tools; each write still needs `confirm=true`. |
| Local console URL / API key | Optional, for a console reached on the LAN (UniFi Network > Settings > Control Plane > Integrations). Available as `console="home"`. |
| Default console | Optional; used when a call omits `console=`. |

## Manual setup (any MCP client)

```bash
claude mcp add unifi --scope user -- uv run --directory /path/to/unifi-mcp unifi-mcp
```

Put keys in `~/.config/unifi-mcp/secrets.env` (`chmod 600`), one `KEY=value` per line, rather than in
the client config. More consoles can be aliased in `~/.config/unifi-mcp/consoles.yaml`
(see `consoles.example.yaml`).

| Variable | Purpose |
|---|---|
| `UNIFI_SM_API_KEY` | Site Manager key (fleet tools and Connector access) |
| `UNIFI_ALLOW_WRITES` | `true` to expose write tools (default off) |
| `UNIFI_LOCAL_URL`, `UNIFI_LOCAL_API_KEY` | One local console, available as `home` (`UNIFI_LOCAL_NAME` renames it; `UNIFI_LOCAL_VERIFY_SSL=true` to verify its certificate) |
| `UNIFI_CONSOLES_FILE` | Alias file (default `~/.config/unifi-mcp/consoles.yaml`) |
| `UNIFI_SECRETS_FILE` | Key file (default `~/.config/unifi-mcp/secrets.env`) |
| `UNIFI_DEFAULT_CONSOLE` | Console used when a call omits `console=` |
| `UNIFI_TIMEOUT` | HTTP timeout in seconds (default 30) |

## Limits worth knowing

- Protect's official API has no event history, so `protect_events` and `protect_license_plates`
  read Protect's internal API (`/protect/api/events`). The cloud Connector forwards it with the
  Site Manager key; it is undocumented and can change between Protect versions. Plate text and
  sightings are personal data: use them within the monitoring scope agreed for each site.

- Connector: 100 requests/minute per console (the client paces itself and retries 429s),
  25 s per request, 10 MB per response. A console that does not answer in time returns
  `408 DeviceTimeout`; a local alias is the fallback.
- Only connected UniFi OS consoles with firmware >= 5.0.3 are reachable through the Connector.
- `/v1/isp-metrics` covers all sites at once (hundreds of KB raw); the tool summarizes by default.

## Development

```bash
uv sync
uv run pytest
```

The API specs are bundled in `src/unifi_mcp/specs/`; refresh them from `developer.ui.com/llms.txt`
when Ubiquiti publishes a new Network version.

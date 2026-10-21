# Changelog

## 0.5.0
- `list_fixed_ips`: DHCP reservations (fixed IPs) and aliases on a site, from the classic `rest/user` + `rest/networkconf`
  (the Integration API does not expose them).
- `set_client_reservations`: set fixed IPs and/or aliases for many clients in one call (create the client record when the
  MAC is unknown). Validates the whole batch first (IP in the network's subnet, not gateway/broadcast, not reserved for
  another MAC, no duplicates) and writes nothing if any item fails; dry-run plan until `confirm=true`, needs
  `UNIFI_ALLOW_WRITES=true`. Fixed IPs may sit outside the DHCP range (verified on UniFi OS Server 5.1).

## 0.4.0
- `find_client`: locate a client by MAC(s), IP or name down to the switch port (classic `stat/sta` via the
  Connector) or AP + SSID, falling back to the Integration API's uplink device. Hint-first search
  (console/site), remembered locations, `groups` mapping in consoles.yaml, then the fleet.

## 0.3.0

- UniFi Protect: `protect_list_cameras`, `protect_list_devices`, `protect_camera_snapshot`,
  `protect_events`, `protect_license_plates`, `protect_api_request`.
- Event and plate history read Protect's internal API through the Connector, windowed by day or hours
  in the NVR's own timezone.
- Protect writes use the same gate as Network writes (flag + dry-run + `confirm=true`).

## 0.2.0

- Installable as a Claude Code plugin (`/plugin install unifi --marketplace troymroberts/unifi-mcp`);
  Claude Code prompts for the Site Manager key, optional local console and write permission, and keeps
  keys in its secure store.
- One local console can be configured from env (`UNIFI_LOCAL_URL` / `UNIFI_LOCAL_API_KEY`) as `home`.
- Keys can be kept in `~/.config/unifi-mcp/secrets.env` instead of the MCP client config.

## 0.1.0

- Fleet tools over the Site Manager API: `list_consoles`, `list_cloud_sites`, `list_cloud_devices`,
  `fleet_health`, `get_isp_metrics` (summarized per site), `cloud_api_get`.
- Per-console Network API tools through the cloud Connector or a local console: sites, devices
  (with statistics), pending devices, clients, and site configuration listings.
- Generic `network_api_request` with spec search (`network_api_endpoints`, `network_api_describe`)
  from the bundled Network 10.6.106 OpenAPI spec.
- Writes off by default; with `UNIFI_ALLOW_WRITES=true`: `restart_device`, `power_cycle_port`,
  `set_guest_access`, `create_vouchers`, plus non-GET `network_api_request`, all dry-run until
  `confirm=true`.
- Console aliases in `consoles.yaml`, name/partial-name console and site resolution, per-console
  rate limiting with 429 retry, actionable 403/408 messages.

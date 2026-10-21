# Changelog

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

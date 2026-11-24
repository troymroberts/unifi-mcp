# Changelog

## 0.8.0
Switch port settings (classic `rest/device` port overrides via the Connector; the Integration API can only
power-cycle a port).
- `list_switch_ports`: one switch's ports with link/speed, name, profile, native network (VLAN), tagged VLANs
  (all/none/custom + excluded), PoE mode/watts, uplink/LAG flags, UniFi devices fed from the port, and the client
  MACs learned on each port (spot an unmanaged switch behind an access port).
- `set_switch_ports`: name, native VLAN (network name, VLAN id or id), tagged VLANs (`all`, `none` or a list of
  networks), port profile, PoE `auto`/`off` for several ports of one switch. Whole batch validated first; refuses
  the switch's uplink, LAG members and ports feeding adopted UniFi devices for VLAN/profile/PoE-off changes; the
  plan shows the clients each change affects; dry-run until `confirm=true`, needs `UNIFI_ALLOW_WRITES=true`.
  Writes the device's full `port_overrides` list with only the requested ports changed.

## 0.7.0
Client DHCP reservations and aliases (classic `rest/user` via the Connector; the Integration API has no
fixed-IP or client-name write). Originally the 0.5.0 `feat/client-reservations` branch, rebased onto 0.6.2.
- `list_fixed_ips`: DHCP reservations (fixed IPs) and aliases on a site, from the classic `rest/user` + `rest/networkconf`
  (the Integration API does not expose them).
- `set_client_reservations`: set fixed IPs and/or aliases for many clients in one call (create the client record when the
  MAC is unknown). Validates the whole batch first (IP in the network's subnet, not gateway/broadcast, not reserved for
  another MAC, not leased right now to another connected device, no duplicates) and writes nothing if any item fails; dry-run plan until `confirm=true`, needs
  `UNIFI_ALLOW_WRITES=true`. Fixed IPs may sit outside the DHCP range (verified on UniFi OS Server 5.1).

## 0.6.2
- `device_health`: SFP/SFP+ DDM optics. A flagged SFP port now carries an `sfp` block (rx/tx optical
  power, temperature, vendor, whatever `sfp*` fields the firmware reports); `include_sfp=true` adds every
  SFP port's optics as `sfp_ports`. Isolates a fibre/connector fault (low Rx power, or low far-end Tx)
  from a failing transceiver (abnormal local Tx) without a site visit. DAC/non-DDM optics report little.

## 0.6.1
Fixes from live validation against a real controller:
- `device_health` no longer flags a port for `rx_dropped`/`tx_dropped` alone — UniFi counts normal flooded
  broadcast/multicast frames there, so almost every access port was being reported. Only real errors,
  STP-blocking and PoE faults are flagged now.
- Satisfaction/experience of `-1` (UniFi's "not computed") is reported as absent instead of a `-1` score,
  in `device_health` radios, `list_wireless_clients` and `find_client`.
- `client_app_usage` gives a clearer note when DPI is reachable but returns nothing ("enable Deep Packet
  Inspection and DPI stats on the site").

## 0.6.0
Wi-Fi + infrastructure diagnostics (all classic-API via the Connector; a console that only answers the
Integration API gets a note instead of data). Version 0.6.0 to avoid colliding with the 0.5.0
`feat/client-reservations` branch — reconcile at merge.
- `find_client`: wireless hits now include `band_ghz` (2.4/5/6), `channel`, `signal_dbm` and UniFi
  `satisfaction` on the access_point.
- `list_wireless_clients`: connected wireless clients with band/channel/signal/experience, filterable by
  `band` (2.4/5/6), `network` (VLAN-name substring) or `ssid` — e.g. "which clients are on 2.4 GHz on the
  voice VLAN".
- `list_events`: diagnostic timeline from `stat/event` — roam (from-AP → to-AP, channel/radio), connect,
  disconnect, switch `port_up`/`port_down` (flapping), `lost_contact`, `restart`, `stp`; device/AP names
  resolved; filter by `macs`, `kinds`, `hours`.
- `device_health`: per-device from `stat/device` — CPU/RAM/temp/uptime/firmware; switch ports with
  errors/drops, STP-blocking or PoE faults; AP per-radio airtime, external interference, TX-retry % and
  client count; `problems_only`.
- `site_health`: `stat/health` per subsystem — WAN latency/drops, throughput, device/client counts.
- `client_app_usage`: per-client DPI (`stat/stadpi`) application/category traffic by bytes, tied to the
  client (for "which client used the most <app>"). DPI reports data volume, not time-on-app; app/category
  ids plus any name the controller supplies.
- Note: `device_health`/`site_health`/`client_app_usage` parse classic schemas defensively (missing fields
  are omitted) and should be sanity-checked against a live controller; app-id→name and a per-app usage
  timeline are follow-ups.

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

# Monitor and logs web UI

Open `http://<proxy_host>:8089/`.

- **Monitor** (`/`): the values the controller reports to the cloud,
  decoded from the proxied traffic and refreshed every 10 s: boiler, buffer,
  heating circuit, outside and flue gas temperatures, O2, burner power and
  state, boiler pump, lambda PWM, fuel counter, the reported setpoints with
  their limits, and the connection state (last exchange, cloud status, last
  value the cloud wrote to the controller). Charts cover the last 1 to 48
  hours; click a name in the temperature chart's legend to hide or show its
  line. Values outside a plausible range (corrupted CAN reads) are dropped.
  An active alarm shows as a red banner at the top, and an *Alarms* table
  lists the alarms seen in the reports (code, state, start and acknowledge time
  from the controller's clock, same as the display's alarm list).
  The history is kept in memory and rebuilt from the logs in the volume when
  the container starts. The same data is available as JSON at
  `/api/monitor?hours=24`.
- **Registers** (`/registers`): a raw table of every register the controller
  reported or the server asked for: number (`reg,idx`), likely use (from
  [register-mapping.md](register-mapping.md) and the service panel
  settings, with how sure the mapping is, or *unknown*; setting ranges and
  names of enumerated values such as modes), decoded value, raw
  value and when it was last seen. A register that shows up for the first
  time, for example one the cloud's service panel adds to the read list, gets
  a new row on its own and is highlighted. A value only counts once two
  reports in a row agree; a different raw value is flagged as a possible
  glitch. The table is kept in `registers.json` in the log volume, so
  registers read only once survive a restart. JSON at `/api/registers`.
- **Heating settings** (`/heating`): the heating controller's settings, grouped like the
  cloud panels: user settings (on/off, boiler target and mode, buffer
  targets, room temperatures, circuit 1 program), boiler (hystereses, pump
  temperature and overrun, return protection), burner (power, blower curve,
  ignition, glowing, burn-out, feeder, fuel, lambda/O2, pause mode) and
  heating circuits CO1 / CO2 (mode, manual target, weather curve). Each shows
  the reported value, its register, when it was read and the allowed range.
  Changing them works like the boiler target (see [operating-modes.md](operating-modes.md))
  and asks for confirmation first. This is the only page that writes to the
  controller; the monitor page only shows values. JSON at `/api/settings`.
- **Pellet batches** (`/batches`): a log of the heating settings used with
  each pellet batch. *Save settings* stores the values shown on
  `/heating` at that moment (read them there first) with a supplier, a date
  (default today) and a free comment. Each saved entry shows how many values
  differ from the entry before it; *Show settings* lists every value next to
  the entry before and the boiler's current value, with differences
  highlighted, and can be narrowed to the changed ones. Supplier, date and
  comment can be edited later; the saved values cannot. Read only: nothing is
  written to the controller. Deleting an entry needs `UI_USER` /
  `UI_PASSWORD`. The entries are kept in `settings-snapshots.json` in the log
  volume, which the log deletion leaves alone. JSON at `/api/snapshots`.
- **Logs** (`/logs`): every log file in the volume with size and date, a
  download link per file and a "Download all (zip)" button. Each `.jsonl`
  file has a *Delete* button, and *Delete all .jsonl* clears them all (both
  ask first). Deleting only works when `UI_USER` / `UI_PASSWORD` are set;
  `state.json` (the server's register state), `registers.json` (the
  register table) and `settings-snapshots.json` (the pellet batches) cannot
  be deleted from here.
  Charts already loaded in the monitor keep their data until the next restart.
- **Settings** (`/settings`): settings of this app itself, kept apart from
  the heating controller's settings on `/heating`. For now it holds the
  *Cloud monitoring* switch (see [cloud-monitoring.md](cloud-monitoring.md)).

The UI is available in English and German. It follows the browser language;
the EN / DE switch at the top right overrides it and is remembered in the
browser. To add a language, copy the `en` block of `I18N` in
`estyma_local/pages/monitor.html`, translate it and add a button to `#lang`.

The logs contain whatever the controller sends (device IDs, PINs, ...). Keep
port 8089 reachable from your LAN only and set `UI_USER` / `UI_PASSWORD`
(they protect the monitor too).

To analyse the logs inside the container instead:

```sh
docker exec <proxy-container> python3 -m estyma_local.analysis.summarize_logs /logs       # endpoints and parameters
docker exec <proxy-container> python3 -m estyma_local.analysis.compare_cloud_proxy /logs  # match cloud values to registers
```

Register meanings found so far with this comparison are in
[register-mapping.md](register-mapping.md). The Polish cloud field
names, their English meaning and enum values are in
[cloud-translation.md](cloud-translation.md). All registers with function and range
are listed in
[registers.md](registers.md).

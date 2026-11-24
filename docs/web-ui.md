# Monitor and logs web UI

Open `http://<proxy_host>:8089/` and log in (see [Login](#login) below).

Every page ends with a footer showing the running version, e.g.
`estyma-local v1.0.0 (3f2a9c1)` (also in `/api/health` and the MQTT
discovery's `origin.sw_version`).

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
  Click the *Fuel counter* tile for the **fuel statistics**: kg (and kWh)
  today, yesterday, this week, month and year, and a bar chart per day,
  week, month or year (calendar periods in `CONTROLLER_TZ`, weeks from
  Monday) with a table view. Periods in which the server was not recording
  for a while are hatched: their fuel is spread evenly over the gap. kWh
  use `FUEL_KWH_PER_KG`, else the controller's calorific value, else 4.8.
  JSON at `/api/fuel?period=day|week|month|year&count=N`.
  Click the *Burner state* tile for the **burner statistics**: starts,
  failed starts, error stops, burner hours and the average burn per start,
  today, yesterday, this week, month and year, with a chart of the starts per
  day, week, month or year (failed starts in red) and a table view. A start
  is every ignition (state 1); it failed when the burner left ignition before
  the flame was established (glowing, modulation or sustain). Burner hours
  count glowing, modulation and sustain. Time in which the server was not
  recording is not counted and those periods are hatched. JSON at
  `/api/burner?period=day|week|month|year&count=N`.
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
  and asks for confirmation first. Below the settings, the **weekly timer**
  of heating circuit 1 shows one row per day with the 24 hours: blue hours
  are comfort, grey ones eco. Click or drag over hours to change them,
  copy Monday to the work days or to every day, then *Save timer*; all 7
  days are written in one go, like the cloud does, and hatched hours wait
  for the controller's read-back. This is the only page that writes to the
  controller; the monitor page only shows values. JSON at `/api/settings`.
- **Pellet batches** (`/batches`): a log of the heating settings used with
  each pellet batch. *Save settings* stores the values shown on
  `/heating` at that moment (read them there first) with a supplier, a date
  (default today), an optional price per tonne and a free comment. Each saved entry shows how many values
  differ from the entry before it; *Show settings* lists every value next to
  the entry before and the boiler's current value, with differences
  highlighted, and can be narrowed to the changed ones. Supplier, date, price
  and comment can be edited later; the saved values cannot. The price holds
  from the entry's date (local midnight) until the next entry with a price,
  so leave it empty on a later save of the same batch; the fuel statistics
  on the monitor (and `/api/fuel`) use it for the cost per day, week, month
  and year. Fuel burnt before the first priced entry has no cost; a period
  that is only partly priced shows "≈". Read only: nothing is
  written to the controller. Deleting an entry needs the admin login. The entries are kept in `settings-snapshots.json` in the log
  volume, which the log deletion leaves alone. JSON at `/api/snapshots`.
- **Logs** (`/logs`): every log file in the volume with size and date, a
  download link per file and a "Download all (zip)" button. Each `.jsonl`
  or `.jsonl.gz` file has a *Delete* button, and *Delete all logs* clears
  them all (both ask first). Downloading and deleting need the admin login;
  `state.json` (the server's register state), `registers.json` (the
  register table), `history.sqlite3` (the register history) and `settings-snapshots.json` (the pellet batches) cannot
  be deleted from here.
  Charts already loaded in the monitor keep their data until the next restart.
- **Settings** (`/settings`): settings of this app itself, kept apart from
  the heating controller's settings on `/heating`. The *Cloud connection*
  section comes first; the *Debugging/Development* section groups the
  *Poll the Estyma cloud* switch (see [cloud-monitoring.md](cloud-monitoring.md)),
  *Keep raw traffic logs (debugging)*, off by default: writes every
  exchange to `exchanges-*.jsonl`, gzipped every hour (about 0.6 MB a day).
  The register history in `history.sqlite3` is kept either way, so this is
  only needed to debug or to decode new registers. The choice is kept in
  `raw-logs.json`; until it is first changed, `RAW_LOGS` decides (default
  `0`). Switching it needs the admin login.
  The same card has *Keep logs for N days (0 = forever)*: with N from 1 to
  3650, every `.jsonl` / `.jsonl.gz` log (`exchanges-*`, `cloud-*`, `writes-*`)
  last written more than N days ago is deleted, right after the change and then
  every hour. `state.json`, `registers.json`, `history.sqlite3` and the
  other state files are never touched. The choice is kept in `log-retention.json` in the log volume;
  until it is first changed, `LOG_RETENTION_DAYS` decides (default `0` = keep
  all logs). Changing it needs the admin login, like deleting logs.
  *Answer when the cloud is slow* sets how many seconds (1 to 60) the proxy
  waits for the cloud before it answers the controller itself, see
  [operating-modes.md](operating-modes.md#cloud-timeout-fallback); switching
  it off waits for the cloud as before. The choice is kept in
  `cloud-fallback.json`; until it is first changed, `CLOUD_FALLBACK_SECONDS`
  decides (default `12`, `0` = off). Changing it needs the admin login.

The UI is available in English and German. It follows the browser language;
the EN / DE switch at the top right overrides it and is remembered in the
browser. To add a language, copy the `en` block of `I18N` in
`estyma_local/pages/monitor.html`, translate it and add a button to `#lang`.

## Login

Every page needs a login (only `/api/health`, for Docker's healthcheck,
answers without one):

- **Admin**: `UI_USER` / `UI_PASSWORD`. If you set neither, the server
  creates a password for the user `admin` on its first start, prints it to
  the container log (`web UI: no UI_USER / UI_PASSWORD set, created a
  login: ...`) and keeps it in `.ui-password` in the logs volume, so it stays
  the same after restarts and updates. Read it again with
  `docker exec <container> cat /logs/.ui-password`. The admin may change
  everything: heating settings, app settings, deleting and downloading logs.
- **Viewer** (optional): `UI_VIEWER_USER` / `UI_VIEWER_PASSWORD`. Sees every
  page and value, but cannot change anything and cannot download logs (they
  hold the controller's IDs). The footer says *(read only)*.

The browser stays logged in for `UI_SESSION_DAYS` (30) days; *Log out* is in
the footer of every page. Sessions survive restarts (`.ui-sessions.json`,
only hashes of the tokens). After 5 failed logins within 15 minutes from one
address, that address is refused for 15 minutes. Scripts can send HTTP basic
auth with each request instead of logging in, e.g.
`curl -u admin:PASSWORD http://host:8089/api/settings`.

The login cannot be switched off.

The logs contain whatever the controller sends (device IDs, PINs, ...). Keep
port 8089 reachable from your LAN only. To reach the UI over HTTPS (for
example from your phone), put it behind a reverse proxy, see
[https.md](https.md).

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

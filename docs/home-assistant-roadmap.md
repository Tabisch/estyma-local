# Roadmap: local server and Home Assistant integration

Long-term goal: run the Estyma controller without the igneo.pl cloud and
integrate it with locally hosted smart home systems, Home Assistant first.

This is a plan, not a spec. Register numbers and protocol details live in
[register-mapping.md](register-mapping.md) and
[cloud-translation.md](cloud-translation.md).

## Where we are (2026-11-08)

Done:

- Transparent logging proxy between controller and cloud (router DNAT), logs
  every `POST /device/connect` exchange.
- Cloud recorder that polls the cloud web API, plus `compare_cloud_proxy.py`
  to line cloud fields up with registers.
- Live monitor on the web UI (port 8089), decoding `ZAPISZ_ODCZYT`.
- Protocol understood for reads: one endpoint, loose `k=v&` form body padded
  with NULs, reply with `ODCZYT` (registers to report), optional `ZAPIS`
  (values to write) and `SERVER_INFO`.
- Most live values, setpoints and the alarm registers are mapped.

Not done yet:

- No local write has ever been seen to change a setpoint. The only `ZAPIS`
  observed is the clock sync (`8549,1=<local unix time>;4112,1=1`).
- The controller still depends on the cloud for every reply.
- Nothing is exposed to Home Assistant; the existing integration goes through
  the cloud via EstymaApi.

## Target architecture

```
controller --DNAT--> estyma-local server --(optional mirror)--> igneo.pl
                         |   decode / validate / store
                         |   write queue (ZAPIS)
                         +--> web UI (:8089)
                         +--> MQTT broker  --> Home Assistant (MQTT discovery)
                         +--> local HTTP/WS API --> native HA integration (later)
```

One service owns the controller session. It answers `/device/connect` itself,
keeps the current register state, and publishes it. Every other consumer (web
UI, MQTT, HA) talks to that service, never to the controller.

Three operating modes, switchable by config, so the move away from the cloud
can be done and undone safely:

| Mode | Who answers the controller | Cloud sees data | Writes from |
|---|---|---|---|
| `proxy` (today) | cloud, forwarded | yes | cloud only |
| `hybrid` | cloud, but the local server may add its own `ZAPIS` / `ODCZYT` entries to the reply | yes | cloud and local |
| `local` | local server | optional one-way mirror, or no | local only |

## Phases

### Phase 1: settle the protocol for writes

Without confirmed writes, the integration is read-only. Steps:

1. Change the boiler target 60 -> 61 in the cloud UI with proxy and recorder
   running; confirm `ZAPIS&;8195,1=61;&KONIEC` and that `8195,1` reports 61
   on the next exchange. Change it back.
2. Repeat for one value of each kind: a /10 value (room comfort `8242`), an
   enum (`8194` mode, `8292` heating program), an on/off flag (`8197`), a
   weekly timer (`8486..8510`). Record the exact encoding (raw vs. /10,
   whether `0x100 +` flag registers are written with or without the 0x100).
3. Find out whether a write needs an acknowledgement: does the controller
   echo the value, set a flag, or does the cloud re-send until it reads the
   new value? Watch the cloud's `/info_check_order` states
   (`nowy -> wyslano -> zrealizowano / blad`) next to the raw exchanges.
4. Learn how alarm reset/acknowledge works remotely (if the cloud offers it).
5. Find out what `POBIERZ_ODCZYT` and `SERVER_INFO=REGISTER,1;DELAY,10`
   control (e.g. does `DELAY,5` make the controller poll faster?).

Exit: a table of writable registers with encoding, limits, and verified
round trip.

### Phase 2: local server core (still behind the cloud)

1. Split the proxy into reusable parts: request parser, reply builder,
   register model (value, min, max, unit, scale, writable, last seen).
2. Validation layer: drop single-report glitches (upper 16 bits garbage,
   `0xA5A5....`, `1000039936 + x`, `100794368 + x`), keep low 16 bits where
   that is known to be safe.
3. Persistent state (SQLite or JSONL replay) so restarts keep the last values
   and history for the monitor.
4. Implement `hybrid` mode: inject a local `ZAPIS` into the cloud's reply.
   This allows testing writes while the cloud stays the source of truth.
5. Tests from recorded exchanges (golden files from the proxy logs, with
   device identifiers replaced).

### Phase 3: local mode (cloud off)

1. Answer `/device/connect` locally: same reply shape, `Content-Type`,
   chunked encoding and line breaks as the cloud (copy byte layout from logs).
2. Serve the `ODCZYT` list ourselves: the 63 base registers plus the latest
   alarm slots (follow the `16460` counter like the cloud does).
3. Clock sync: send `8549,1=<local unix time>;4112,1=1` on start and then
   periodically. Investigate the ~45 minute alarm timestamp offset.
4. Fallback: if the local server is down the controller loses its server
   (the DNAT still points to it). Document how to switch the DNAT back, and
   consider a watchdog that forwards to the cloud when local handling fails.
5. Optional cloud mirror: keep forwarding to the cloud in the background and
   discard its `ZAPIS`, so the vendor app keeps showing data. Decide per
   install.
6. Run in `local` mode for a few weeks before relying on it.

### Phase 4: Home Assistant via MQTT discovery (first integration)

Recommended first, because it works with HA, ioBroker, openHAB, Node-RED and
others without writing HA code, and fits the existing Docker deployment.

1. Add an MQTT client (paho-mqtt or a small stdlib implementation) with
   config `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD`,
   `MQTT_PREFIX` (default `estyma`), `HA_DISCOVERY_PREFIX` (default
   `homeassistant`).
2. Topics:
   - `estyma/<device>/state` one JSON payload with all decoded values
     (retained), entities use `value_template`.
   - `estyma/<device>/availability` `online` / `offline` (LWT, and offline
     when the controller has not reported for e.g. 90 s).
   - `estyma/<device>/set/<key>` command topics for writable values.
   - `estyma/<device>/alarm` alarm slots as JSON.
3. Publish discovery configs (retained) for one HA device with all entities
   (see entity design). Use a stable `unique_id` derived from a hash of the
   controller ID, never the raw ID or PIN.
4. Writes from MQTT go into the same write queue as the web UI and pass the
   safety checks below.

### Phase 5: native Home Assistant integration (optional, later)

Only worth it if MQTT discovery is not enough (config flow UI, diagnostics,
HACS distribution, no broker needed).

1. Expose a small local API from the server: `GET /api/state`, `POST
   /api/set`, and a WebSocket or SSE stream for pushes.
2. Either extend the existing EstymaApi wrapper with a "local" backend that
   talks to this API, or write a new `estyma_local` custom component with a
   `DataUpdateCoordinator` fed by the push stream.
3. Config flow: host, port, optional credentials; zeroconf/mDNS discovery as
   a nice-to-have.
4. Diagnostics download with identifiers redacted.

## Entity design

One HA device "Estyma boiler" (model from `8485` device type, firmware from
`SOFT_VER`). Names in English (from cloud-translation.md), keys stable.

| Entity | HA platform | Source | Notes |
|---|---|---|---|
| Boiler temperature | sensor, temperature, °C | 8193,1 /10 | |
| Boiler calculated target | sensor, °C | 8196,1 | |
| Boiler target | number (min/max from idx 3/4), °C | 8195,1 | writable after phase 1 |
| Buffer top / bottom | sensor, °C | 8466,1 / 8467,1 /10 | |
| Buffer top / bottom target | number, °C | 8468 / 8469 | writable after phase 1 |
| Heating circuit 1 temperature | sensor, °C | 16385,1 /10 | |
| Outside temperature | sensor, °C | 8543,1 /10 | |
| Flue gas temperature | sensor, °C | 8473,1 low byte | |
| Flue gas oxygen | sensor, % | 8480,1 /100 | |
| Burner power | sensor, % | 16389,1 low byte | |
| Lambda PWM | sensor, diagnostic | 16391,1 low byte | |
| Burner status | sensor, enum (off, ignition, glowing, modulating, cleaning, error, unknown N) | 8199,1 low byte | |
| Boiler pump / circuit 1 pump | binary_sensor, running | 8823 / 8857 low byte | |
| Controller on | switch (or binary_sensor until writes are proven) | 8197,1 low byte | needs extra care, see safety |
| Operation mode | select | 8194,1 | values still to map |
| Heating program circuit 1 | select | 8292,1 (probable) | |
| Room comfort / eco target | number, °C, step 0.1 | 8242 / 8264 /10 | |
| Fuel total | sensor, kg, `state_class: total_increasing` | 8484,1 /10 | works with HA statistics; daily use comes for free via utility_meter |
| Fuel energy | sensor, kWh, total_increasing (optional) | fuel kg x calorific value | calorific value as config (4.8 kWh/kg default from burner settings); gives the HA Energy dashboard |
| Alarm active | binary_sensor, problem | 16460 counter + slot state | |
| Alarm code / since | sensor | alarm slot | code text table to build |
| Last report / connection | sensor timestamp, diagnostic; availability | server | |
| Weekly timers circuit 1 | later: attributes or a custom card | 8486..8510 | encoding still unknown |

Rules:

- Only create entities for fitted hardware (number of circuits, buffers,
  lambda, solar flags), so installs without a buffer do not get dead entities.
- Mark settings as `entity_category: config` and raw/diagnostic values as
  `diagnostic`.
- Unavailable instead of stale: all entities follow the availability topic.

## Writes, setpoints and safety

A heating boiler is not a light switch. Writes are the riskiest part.

1. Allow-list: only registers verified in phase 1 are writable. Anything else
   is rejected, regardless of where the request comes from.
2. Range checks: clamp to the controller's own min/max (idx 3/4) and to a
   stricter local limit where sensible (e.g. boiler target never above 80).
3. Rate limiting and de-duplication: HA automations can fire repeatedly; at
   most one pending write per register, coalesce to the latest value, minimum
   interval between writes.
4. Read-back confirmation: a write is only "done" when the controller reports
   the new value; otherwise retry a few times, then mark failed and log it.
   HA shows the reported value, not the requested one.
5. Separate switch for dangerous actions (controller on/off, burner enable):
   off by default (`ALLOW_POWER_WRITES=0`).
6. Global read-only switch (`WRITES_ENABLED=0` default) until phase 1 is done.
7. Audit log of every write (source, old value, new value, result) in the
   log volume.
8. Never send writes while an alarm is active, unless it is the alarm reset
   itself.
9. Authentication for the web UI and MQTT (broker credentials), since the
   server can now change heating settings on the LAN.

## Migration from the cloud integration

The existing HA integration uses EstymaApi against the cloud. Plan:

1. Run both side by side (proxy/hybrid mode keeps the cloud working), compare
   values in HA.
2. Keep entity meaning compatible: provide a mapping table from the old
   wrapper entity IDs (e.g. `temp_boiler_sub1`) to the new ones, so users can
   rename entities and keep history. Fix the wrapper's mistranslations
   (`curcuit`, `lamda`, `obli`, see cloud-translation.md) only in the new
   names, not by breaking the old integration.
3. Fuel day consumption: the cloud computes it from its midnight counter;
   locally use an HA `utility_meter` on Fuel total (or compute it in the
   server).
4. Switch to `local` mode once writes and alarms work locally; the old cloud
   integration then goes unavailable and can be removed.
5. Optionally add a local backend to EstymaApi so the existing integration
   keeps working by just pointing it at the local server.

## Open protocol unknowns

| Unknown | Why it matters | How to find out |
|---|---|---|
| Write encoding and acknowledgement | no setpoints without it | phase 1 test changes |
| `4112,1=1` meaning | may be needed after every write, not only clock sync | writes with and without it in hybrid mode |
| `POBIERZ_ODCZYT` | maybe a controller-initiated read request | watch during cloud UI actions |
| `SERVER_INFO` (`REGISTER`, `DELAY`) | registration state, poll interval | vary `DELAY` in local/hybrid mode |
| Unrequested registers (burner settings, DHW, circuits 2-4) | more entities | add candidate registers to `ODCZYT` and look for known values (needs go-ahead) |
| Registers `16392`, `8471` never returned | unknown whether harmful | ignore for now |
| Burner states 3, 4, 6, 8, 9 | enum completeness | keep recording |
| Operation mode / heating program values | select options | change in cloud UI |
| Timer bitmask encoding and weekday order | timer support | change one hour of Monday |
| Alarm codes besides 140 | alarm texts | collect over time, vendor manual |
| Alarm time offset (~45 min) | correct timestamps | compare controller clock after sync |
| Behaviour when the server is unreachable for long | safety of local mode | stop local server briefly, watch the controller |
| Firmware updates via the cloud | could change the protocol | keep logging; pin behaviour per `SOFT_VER` |
| Other device types / firmware versions | others using the project | collect anonymised logs from other users |

## Suggested order of work

1. Phase 1 write tests (needs Tabisch to make changes in the cloud UI).
2. Register model + validation + tests (phase 2), can run in parallel.
3. MQTT publisher read-only (phase 4 steps 1-3): useful immediately, even in
   proxy mode.
4. Hybrid writes with safety layer, then MQTT command topics.
5. Local mode, then cloud off.
6. Native integration / EstymaApi local backend if still wanted.

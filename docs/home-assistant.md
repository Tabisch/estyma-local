# Home Assistant (MQTT)

The server can publish everything it knows about the controller to your MQTT
broker, with Home Assistant's MQTT discovery, so one **Estyma boiler** device
with all its entities appears in Home Assistant by itself. By default it is
**read-only**; with `MQTT_WRITES=1` Home Assistant can also change the
settings whose writes were tested on the real controller (see
[Controls](#controls)). Reading works in every mode (`proxy` included),
because it only uses what the controller reports.

Set it up:

1. In Home Assistant, the MQTT integration must be connected to the same
   broker (with discovery on, the default).
2. In the stack's environment variables (Portainer) set `MQTT_HOST` and, if
   your broker needs a login, `MQTT_USER` / `MQTT_PASSWORD`. Redeploy.
3. The container log shows `mqtt: connected to ...`. The device appears under
   *Settings → Devices & services → MQTT* within one report (about 20 s).

Entities:

| Entity | Source |
|---|---|
| Boiler, buffer top/bottom, heating circuit 1 flow, outside, flue gas temperatures | 8193, 8466, 8467, 16385, 8543, 8473 |
| Boiler target, boiler target (calculated) | 8195, 8196 |
| Oxygen in flue gas, burner power, burner state (off, ignition, glowing, modulation, burn-out, cleaning, sustain, error) | 8480, 16389, 8199 |
| Boiler on, boiler pump, heating circuit 1 pump (on/off) | 8197, 8823, 8857 |
| Fuel consumed (kg), fuel energy (kWh) | 8484 |
| Fuel used today, fuel used yesterday (kg, calendar days in `CONTROLLER_TZ`) | 8484 via the history store |
| Fuel cost today, fuel cost yesterday (EUR) | the above × the pellet batch price per tonne |
| Burner starts today, failed burner starts today, burner hours today, burner starts yesterday, average burn yesterday (min) | 8199 via the history store |
| Alarm (problem on/off, newest alarm slot as attributes), alarm code, alarm text (e.g. "start failure"; `none` when no alarm) | 16400..16460 |
| Last report, server mode, lambda PWM (diagnostic; lambda PWM disabled by default) | |
| Heating circuit 1 program, room comfort / eco temperature, buffer top / bottom target | 8292, 8242, 8264, 8468, 8469 |
| Weekly timer circuit 1, Monday .. Sunday (comfort hours, e.g. `06-22`) | 8486..8510 |

*Fuel energy* is fuel consumed × calorific value, for the **Energy
dashboard** (add it under *Gas consumption* or as an individual device). The
calorific value is `FUEL_KWH_PER_KG` if set, otherwise the controller's own
setting (8850, read in `hybrid`/`local` mode), otherwise 4.8 kWh/kg. Changing
it makes the total jump, so set it once. A fuel counter reading that goes
down or jumps implausibly is treated as a corrupted read and skipped.

*Fuel used today* starts at 0 at local midnight (`total_increasing`, so
Home Assistant's long-term statistics handle the daily reset); *fuel used
yesterday* holds the previous day's total, handy for a morning notification.
Both come from the register history (`history.sqlite3`): fuel burnt while
the server was not recording is spread evenly over the gap. The same numbers
per day, week, month and year are at `/api/fuel` in the web UI.
*Burner starts* count every ignition, *failed* ones left ignition before
the flame was established; *burner hours* are the time in glowing,
modulation or sustain, and *average burn* is burner hours per successful
start. The today values reset at midnight (`total_increasing`). Statistics
per day, week, month and year are at `/api/burner`.
*Fuel cost today / yesterday* price that fuel with the price per tonne of
the pellet batch in effect that day (entered on `/batches`); they are
*unknown* until a batch has a price.
Other values keep their last valid reading for up to 10 minutes when a
single report carries a corrupted value, so entities don't flicker to
*unknown*.

The device goes *unavailable* when the controller has not reported for
`MQTT_STALE_SECONDS`, or when the server stops (MQTT last will).

Topics, with the defaults (`estyma/boiler`):

- `estyma/boiler/state`: one JSON object with all values (retained). Unknown
  values are `null`. Handy for Node-RED, ioBroker or openHAB as well.
- `estyma/boiler/alarm`: newest alarm slot (`active`, `code`, `text`,
  `state`, `start`, `end`; times in controller clock).
- `estyma/boiler/availability`: `online` / `offline`.
- `homeassistant/<component>/estyma_boiler/<key>/config`: discovery
  (retained), sent again when Home Assistant restarts.

## Controls

With `MQTT_WRITES=1` these become controls instead of sensors:

| Entity | Type | Command payload |
|---|---|---|
| Boiler target | number (box), 1 °C steps, controller's min/max | `62` |
| Boiler on | switch, only with `ALLOW_POWER_WRITES=1` | `ON` / `OFF` |
| Heating circuit 1 program | select | `off`, `economical`, `comfortable` |
| Room comfort / eco temperature, circuit 1 | number (box), 0.1 °C steps | `21.5` |
| Buffer top / bottom target | number (box), config | `55` |
| Weekly timer circuit 1, Monday .. Sunday | text, config | `06-22`, `06-09,16-22`, empty = eco all day |

They need the server to allow writes as well: `MODE=hybrid` or `local` and
`WRITES_ENABLED=1` (on/off also `ALLOW_POWER_WRITES=1`). Every command goes
through the same write queue and checks as the heating settings page:
ranges, at most one change per setting every `WRITE_MIN_INTERVAL` seconds,
nothing but on/off while an alarm is active, read-back from the controller.
The entity shows the value the controller reports, so after a change it
switches to the new value with the controller's next report (about 20 s).
Commands that come faster than `WRITE_MIN_INTERVAL` are not lost: the newest
one is sent once the interval is over. A weekly timer day is written
together with the other 6 days, like the cloud does.

The result of every command is published on `estyma/boiler/write` (not
retained), e.g. `{"key": "boiler_target", "value": 62, "ok": true,
"status": "pending", "message": "Boiler target = 62 °C queued, ..."}` or
`"ok": false` with the reason. Every write is also in `writes-*.jsonl`
with source `mqtt`. Retained messages on the command topics are ignored, so
a command left on the broker never changes the heating after a reconnect.

Anyone who can publish to the broker can then change these settings: give
the broker a login (`MQTT_USER` / `MQTT_PASSWORD`) and do not allow
anonymous clients. Command topics: `estyma/boiler/set/<key>`, keys
`boiler_target`, `boiler_on`, `circuit1_program`, `room_comfort_1`,
`room_eco_1`, `buffer_top_target`, `buffer_bottom_target`,
`co1_timer_mon` .. `co1_timer_sun`.

Example automation: eco on weekday mornings when nobody is home.

```yaml
automation:
  - alias: Heating eco while away
    triggers:
      - trigger: state
        entity_id: group.family
        to: not_home
    actions:
      - action: select.select_option
        target:
          entity_id: select.estyma_boiler_heating_circuit_1_program
        data:
          option: economical
```

To remove the device, delete it in Home Assistant and clear the retained
`homeassistant/+/estyma_boiler/+/config` topics (e.g. with MQTT Explorer).

All variables are listed in [configuration.md](configuration.md#mqtt--home-assistant). The plan for writes from Home Assistant is in [home-assistant-roadmap.md](home-assistant-roadmap.md).

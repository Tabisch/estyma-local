# Home Assistant (MQTT)

The server can publish everything it knows about the controller to your MQTT
broker, with Home Assistant's MQTT discovery, so one **Estyma boiler** device
with all its entities appears in Home Assistant by itself. It is
**read-only**: nothing from MQTT reaches the controller yet. It works in every
mode (`proxy` included), because it only uses what the controller reports.

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
| Oxygen in flue gas, burner power, burner state (off, ignition, glowing, modulation, cleaning, sustain, error) | 8480, 16389, 8199 |
| Boiler on, boiler pump, heating circuit 1 pump (on/off) | 8197, 8823, 8857 |
| Fuel consumed (kg), fuel energy (kWh) | 8484 |
| Alarm (problem on/off, newest alarm slot as attributes), alarm code, alarm text (e.g. "start failure"; `none` when no alarm) | 16400..16460 |
| Last report, server mode, lambda PWM (diagnostic; lambda PWM disabled by default) | |

*Fuel energy* is fuel consumed × calorific value, for the **Energy
dashboard** (add it under *Gas consumption* or as an individual device). The
calorific value is `FUEL_KWH_PER_KG` if set, otherwise the controller's own
setting (8850, read in `hybrid`/`local` mode), otherwise 4.8 kWh/kg. Changing
it makes the total jump, so set it once. A fuel counter reading that goes
down or jumps implausibly is treated as a corrupted read and skipped.
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

To remove the device, delete it in Home Assistant and clear the retained
`homeassistant/+/estyma_boiler/+/config` topics (e.g. with MQTT Explorer).

All variables are listed in [configuration.md](configuration.md#mqtt--home-assistant). The plan for writes from Home Assistant is in [home-assistant-roadmap.md](home-assistant-roadmap.md).

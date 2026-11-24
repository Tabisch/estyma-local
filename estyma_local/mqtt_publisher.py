"""MQTT publisher with Home Assistant discovery, and optional controls.

Publishes what the server knows about the controller (the register state of
``controller.Controller``) to an MQTT broker, so Home Assistant (or anything
else that speaks MQTT) can show it.

With ``MQTT_WRITES=1`` (and writes allowed on the server: ``MODE`` hybrid or
local, ``WRITES_ENABLED=1``) the settings whose writes were confirmed on the
real controller also get command topics: boiler target, heating program,
room comfort / eco, buffer targets, the weekly timer and, with
``ALLOW_POWER_WRITES=1``, boiler on/off. Commands go through the same write
queue and safety checks as the web UI (source ``mqtt``). Retained commands
are ignored, so a stale message on the broker never changes the heating.

Topics (``<p>`` = ``MQTT_PREFIX``/``MQTT_DEVICE_ID``, default ``estyma/boiler``):

* ``<p>/state``        one JSON object with every value (retained), unknown
                       values are ``null``
* ``<p>/alarm``        the newest alarm slot as JSON (retained)
  (``fuel_today`` / ``fuel_yesterday``: kg burnt per calendar day in
  ``CONTROLLER_TZ``, from the history store, see fuel_stats.py;
  ``fuel_cost_today`` / ``fuel_cost_yesterday``: their cost in EUR from the
  pellet batch prices, null without a price)
* ``<p>/set/<key>``    commands (only with ``MQTT_WRITES=1``), e.g.
                       ``estyma/boiler/set/boiler_target`` ``62``
* ``<p>/write``        result of each command as JSON (not retained)
* ``<p>/availability`` ``online`` / ``offline`` (retained). Offline when the
                       controller has not reported for ``MQTT_STALE_SECONDS``,
                       and as last will when the server goes away.
* ``<HA_DISCOVERY_PREFIX>/<component>/estyma_<device id>/<key>/config``
  discovery configs for one "Estyma boiler" device (retained). They are sent
  again whenever Home Assistant announces ``online`` on
  ``<HA_DISCOVERY_PREFIX>/status``.

Configuration (environment variables):

  MQTT_HOST            broker host name or IP; empty = publisher off
  MQTT_PORT            default 1883 (8883 with MQTT_TLS=1)
  MQTT_USER / MQTT_PASSWORD   broker login (optional)
  MQTT_TLS             1 = connect with TLS (system CA certificates)
  MQTT_PREFIX          topic prefix (default estyma)
  MQTT_DEVICE_ID       device part of topics and unique IDs (default boiler)
  HA_DISCOVERY_PREFIX  Home Assistant discovery prefix (default homeassistant)
  MQTT_STALE_SECONDS   seconds without a report until offline (default 300; the controller
                       waits ~100 s after a slow or failed cloud reply)
  FUEL_KWH_PER_KG      calorific value for the energy sensor; default: the
                       controller's own setting (8850) if read, else 4.8
  MQTT_WRITES          1 = command topics for the tested settings (default 0)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import catalog as CAT
from . import controller as CTL
from . import decode as D
from . import fuel_stats as FS
from . import monitor as M
from . import protocol as P
from .version import label as version_label

REPO_URL = "https://github.com/Tabisch/estyma-local"
DEFAULT_KWH_PER_KG = FS.DEFAULT_KWH_PER_KG


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

_TEMP = {"device_class": "temperature", "unit_of_measurement": "°C",
         "state_class": "measurement", "suggested_display_precision": 1}
_DIAG = {"entity_category": "diagnostic"}

# component, key (JSON field), name, extra discovery fields
ENTITIES = [
    ("sensor", "boiler", "Boiler temperature", _TEMP),
    ("sensor", "boiler_calc", "Boiler target (calculated)", dict(_TEMP, suggested_display_precision=0)),
    ("sensor", "boiler_target", "Boiler target", dict(_TEMP, suggested_display_precision=0,
                                                       icon="mdi:thermometer-check")),
    ("sensor", "buffer_top", "Buffer top temperature", _TEMP),
    ("sensor", "buffer_bottom", "Buffer bottom temperature", _TEMP),
    ("sensor", "circuit1", "Heating circuit 1 flow temperature", _TEMP),
    ("sensor", "outside", "Outside temperature", _TEMP),
    ("sensor", "flue", "Flue gas temperature", dict(_TEMP, suggested_display_precision=0)),
    ("sensor", "o2", "Oxygen in flue gas", {"unit_of_measurement": "%", "state_class": "measurement",
                                            "suggested_display_precision": 2, "icon": "mdi:molecule"}),
    ("sensor", "power", "Burner power", {"unit_of_measurement": "%", "state_class": "measurement",
                                         "icon": "mdi:fire"}),
    ("sensor", "burner_state", "Burner state", {"icon": "mdi:fire-circle"}),
    ("sensor", "fuel_total", "Fuel consumed", {"device_class": "weight", "unit_of_measurement": "kg",
                                               "state_class": "total_increasing",
                                               "suggested_display_precision": 1}),
    ("sensor", "fuel_today", "Fuel used today", {"device_class": "weight", "unit_of_measurement": "kg",
                                                 "state_class": "total_increasing",
                                                 "suggested_display_precision": 0,
                                                 "icon": "mdi:calendar-today"}),
    ("sensor", "fuel_yesterday", "Fuel used yesterday", {"device_class": "weight",
                                                         "unit_of_measurement": "kg",
                                                         "suggested_display_precision": 0,
                                                         "icon": "mdi:calendar-arrow-left"}),
    ("sensor", "fuel_cost_today", "Fuel cost today", {"device_class": "monetary", "unit_of_measurement": "EUR",
                                                       "suggested_display_precision": 2,
                                                       "icon": "mdi:cash"}),
    ("sensor", "fuel_cost_yesterday", "Fuel cost yesterday", {"device_class": "monetary",
                                                               "unit_of_measurement": "EUR",
                                                               "suggested_display_precision": 2,
                                                               "icon": "mdi:cash"}),
    ("sensor", "energy", "Fuel energy", {"device_class": "energy", "unit_of_measurement": "kWh",
                                         "state_class": "total_increasing",
                                         "suggested_display_precision": 0}),
    ("binary_sensor", "boiler_on", "Boiler on", {"device_class": "power"}),
    ("binary_sensor", "boiler_pump", "Boiler pump", {"device_class": "running", "icon": "mdi:pump"}),
    ("binary_sensor", "circuit1_pump", "Heating circuit 1 pump", {"device_class": "running",
                                                                  "icon": "mdi:pump"}),
    ("binary_sensor", "alarm", "Alarm", {"device_class": "problem"}),
    ("sensor", "alarm_code", "Alarm code", {"icon": "mdi:alert-circle-outline"}),
    ("sensor", "alarm_text", "Alarm text", {"icon": "mdi:alert-circle-outline"}),
    ("sensor", "lambda_pwm", "Lambda PWM", dict(_DIAG, state_class="measurement",
                                                enabled_by_default=False)),
    ("sensor", "last_report", "Last report", dict(_DIAG, device_class="timestamp")),
    ("sensor", "mode", "Server mode", dict(_DIAG, icon="mdi:server-network")),
]


# Settings Home Assistant can change with MQTT_WRITES=1 (all with a write
# confirmed on the real controller). Without it, or while writes are not
# allowed, they are read-only sensors. key = controller.WRITABLE key = JSON
# field. Fields: control component, read-only component, name, extra
# discovery fields, enum values offered (None = all).
_CFG = {"entity_category": "config"}
_DAY_NAMES = dict(zip(CTL.DAY_KEYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                     "Saturday", "Sunday")))
# Comfort hours as ranges, end exclusive: "06-22", "06-09,16-22", "" = eco all day.
TIMER_PATTERN = r"^$|^none$|^([01]?[0-9]|2[0-4])(:00)?-([01]?[0-9]|2[0-4])(:00)?(, *([01]?[0-9]|2[0-4])(:00)?-([01]?[0-9]|2[0-4])(:00)?)*$"
CONTROLS = {
    "boiler_target": ("number", "sensor", "Boiler target", dict(
        _TEMP, suggested_display_precision=0, icon="mdi:thermometer-check", mode="box"), None),
    "boiler_on": ("switch", "binary_sensor", "Boiler on", {"device_class": "switch"}, None),
    # 2 ("temporary") was never written: not offered.
    "circuit1_program": ("select", "sensor", "Heating circuit 1 program",
                         {"icon": "mdi:home-thermometer"}, (0, 1, 3)),
    "room_comfort_1": ("number", "sensor", "Room comfort temperature, circuit 1",
                       dict(_TEMP, icon="mdi:sun-thermometer", mode="box"), None),
    "room_eco_1": ("number", "sensor", "Room eco temperature, circuit 1",
                   dict(_TEMP, icon="mdi:snowflake-thermometer", mode="box"), None),
    "buffer_top_target": ("number", "sensor", "Buffer top target", {
        **_TEMP, **_CFG, "suggested_display_precision": 0, "mode": "box"}, None),
    "buffer_bottom_target": ("number", "sensor", "Buffer bottom target", {
        **_TEMP, **_CFG, "suggested_display_precision": 0, "mode": "box"}, None),
    **{k: ("text", "sensor", f"Weekly timer circuit 1, {_DAY_NAMES[d]}",
           dict(_CFG, icon="mdi:calendar-clock", pattern=TIMER_PATTERN, max=64), None)
       for d, k in zip(CTL.DAY_KEYS, CTL.TIMER_KEYS)},
}
# Read-only sensors from the table above that ENTITIES already has.
_IN_ENTITIES = {"boiler_target", "boiler_on"}


def _onoff(v):
    return None if v is None else ("ON" if v else "OFF")


class StateBuilder:
    """Controller registers -> the JSON published on ``<p>/state``.

    Keeps the fuel counter monotonic: a lower reading, or a jump larger than
    the boiler can burn, is a corrupted read and keeps the last good value,
    so Home Assistant's energy statistics never see a fake reset.
    """

    MAX_KG_PER_HOUR = 20.0

    def __init__(self, kwh_per_kg: float | None = None, fuel=None) -> None:
        self.kwh_per_kg = kwh_per_kg
        self.fuel = fuel                  # fuel_stats.FuelStats, for today / yesterday
        self._fuel: tuple | None = None   # (ts, kg) last accepted
        self._good = D.LastGood(self.HOLD_SECONDS)

    @staticmethod
    def _value(regs: dict, reg: int):
        v = regs.get((reg, 1))
        return None if v is None else D.value(reg, v[1])

    def _live(self, regs: dict, key: str):
        return self._value(regs, M.KEY_REG[key])

    def _fuel_total(self, regs: dict):
        v = regs.get((8484, 1))
        kg = D.value(8484, v[1]) if v else None
        if kg is None:
            return self._fuel[1] if self._fuel else None
        ts = v[0]
        if self._fuel is not None:
            last_ts, last = self._fuel
            hours = max(0.0, ts - last_ts) / 3600
            if kg < last or kg - last > 50 + self.MAX_KG_PER_HOUR * hours:
                return last
        self._fuel = (ts, kg)
        return kg

    def calorific_value(self, ctl) -> float:
        return self.kwh_per_kg or FS.calorific_value(ctl, env={})

    def alarm(self, ctl) -> dict:
        slot = ctl.alarm_slot()
        out = {"active": bool(ctl.alarm_active()), "slot": slot, "code": None, "text": None,
               "state": None, "start": None, "end": None}
        if slot is None:
            return out
        entry = D.alarm_code(ctl.raw(P.ALARM_CODE_BASE + slot))
        if entry is not None:
            code, state = entry
            out.update(code=code, text=CAT.ALARM_TEXT.get(code), state=state)
        # Controller clock: local wall time encoded as Unix time, shown as is.
        for key, base in (("start", P.ALARM_START_BASE), ("end", P.ALARM_END_BASE)):
            t = D.alarm_time(ctl.raw(base + slot))
            if t == 0 and key == "end":
                out[key] = 0
            elif t:
                out[key] = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return out

    def build(self, ctl) -> tuple[dict, dict]:
        """(state, alarm) from the controller's current registers."""
        with ctl._lock:
            regs = dict(ctl.registers)
            alarm = self.alarm(ctl)
            target = ctl.current("boiler_target")
            on = ctl.current("boiler_on")
            last = ctl.last_report
            mode = ctl.mode
        settings = self._settings(ctl)
        st = {k: self._live(regs, k) for k in
              ("boiler", "boiler_calc", "buffer_top", "buffer_bottom", "circuit1", "outside",
               "flue", "o2", "power", "lambda_pwm")}
        bs = self._live(regs, "burner_state")
        st["burner_state"] = (None if bs is None else
                              CAT.VALUES[8199].get(int(bs), (f"state {int(bs)}",))[0])
        st["burner_state_code"] = None if bs is None else int(bs)
        st["boiler_target"] = target if D.value(8195, target) else None
        st["boiler_on"] = _onoff(on if on in (0, 1) else None)
        pump = self._live(regs, "boiler_pump")
        st["boiler_pump"] = _onoff(None if pump is None else int(pump))
        c1 = self._value(regs, 8857)
        st["circuit1_pump"] = _onoff(None if c1 is None else int(c1))
        fuel = self._fuel_total(regs)
        st["fuel_total"] = fuel
        kwh = self.calorific_value(ctl)
        st["energy"] = round(fuel * kwh, 1) if fuel is not None else None
        st["kwh_per_kg"] = kwh
        day = self.fuel.summary() if self.fuel is not None else {}
        st["fuel_today"] = day.get("today")
        st["fuel_yesterday"] = day.get("yesterday")
        cost = self.fuel.cost_summary() if self.fuel is not None else {}
        st["fuel_cost_today"] = (cost.get("today") or {}).get("cost")
        st["fuel_cost_yesterday"] = (cost.get("yesterday") or {}).get("cost")
        st["alarm"] = "ON" if alarm["active"] else "OFF"
        st["alarm_code"] = alarm["code"] if alarm["active"] else 0
        if not alarm["active"]:
            st["alarm_text"] = "none"
        elif alarm["code"] is None:
            st["alarm_text"] = "alarm"
        else:
            st["alarm_text"] = alarm["text"] or f"code {alarm['code']}"
        st["last_report"] = (datetime.fromtimestamp(last, timezone.utc).isoformat(timespec="seconds")
                             if last else None)
        st["mode"] = mode
        st.update(settings)
        self._hold(st, last)
        return st, alarm

    @staticmethod
    def _settings(ctl) -> dict:
        """The CONTROLS values (except those built above) as Home Assistant shows them."""
        out = {}
        with ctl._lock:
            for key in CONTROLS:
                if key in _IN_ENTITIES:
                    continue
                w = CTL.WRITABLE[key]
                if w.kind == "mask":
                    out[key] = CTL.timer_text(ctl.current(key))
                    continue
                v = D.value(w.reg, ctl.raw(w.reg, w.idx))
                if v is not None and w.options:
                    v = w.options.get(int(v), (None,))[0]
                out[key] = v
        return out

    # A single corrupted CAN read (e.g. 8197 reported as 0x0001 instead of
    # 0x101) decodes to None. Keep the last valid value for HOLD_SECONDS so
    # the entity does not flicker to "unknown" for one report (decode.LastGood).
    HOLD_SECONDS = 600
    _NO_HOLD = ("alarm", "alarm_code", "last_report", "mode", "kwh_per_kg")

    def _hold(self, st: dict, now) -> None:
        now = now or time.time()
        for k, v in st.items():
            if k not in self._NO_HOLD:
                st[k] = self._good.get(k, v, now)


def control_limits(ctl, key: str) -> dict:
    """Discovery fields that follow the controller's own min/max."""
    w = CTL.WRITABLE[key]
    lo, hi = ctl.limits(key)
    if w.options:
        allowed = CONTROLS[key][4]
        return {"options": [w.options[v][0] for v in sorted(w.options)
                            if lo <= v <= hi and (allowed is None or v in allowed)]}
    if w.kind == "int":
        return {"min": w.show(lo), "max": w.show(hi), "step": 1 / w.scale}
    return {}


def discovery(device_id: str, base: str, disc_prefix: str, controls: dict | None = None
              ) -> list[tuple[str, str]]:
    """Retained (topic, payload) pairs announcing the device to Home Assistant.

    ``controls``: key -> extra fields (``control_limits``) of the settings
    that get a command topic; the others are read-only sensors. The config
    of the variant not in use is cleared (empty payload), so switching
    MQTT_WRITES on or off does not leave a dead entity behind.
    """
    controls = controls or {}
    node = f"estyma_{device_id}"
    device = {"identifiers": [node], "name": "Estyma boiler", "manufacturer": "Estyma",
              "model": "Boiler controller (local server)"}
    origin = {"name": "estyma-local", "sw_version": version_label(), "url": REPO_URL}

    def config(comp, key, name, extra):
        cfg = {"name": name, "unique_id": f"{node}_{key}", "state_topic": f"{base}/state",
               "value_template": "{{ value_json.%s }}" % key,
               "availability_topic": f"{base}/availability", "device": device, "origin": origin}
        if key == "alarm":
            cfg["json_attributes_topic"] = f"{base}/alarm"
        cfg.update(extra)
        return (f"{disc_prefix}/{comp}/{node}/{key}/config", json.dumps(cfg, ensure_ascii=False))

    out = [config(comp, key, name, extra) for comp, key, name, extra in ENTITIES
           if key not in controls]
    for key, (comp, ro, name, extra, _) in CONTROLS.items():
        if key in controls:
            cfg = dict(extra, command_topic=f"{base}/set/{key}", **controls[key])
            if comp == "switch":
                cfg.pop("device_class", None)
                cfg.update(payload_on="ON", payload_off="OFF", icon="mdi:power")
            if comp in ("text", "select"):
                for k in ("unit_of_measurement", "state_class", "device_class",
                          "suggested_display_precision"):
                    cfg.pop(k, None)
            if comp != "number":
                cfg.pop("mode", None)
            out.append(config(comp, key, name, cfg))
            out.append((f"{disc_prefix}/{ro}/{node}/{key}/config", ""))   # read-only variant
        else:
            if key not in _IN_ENTITIES:
                # (a sensor cannot be entity_category "config")
                ro_extra = {k: v for k, v in extra.items()
                            if k not in ("mode", "pattern", "max", "entity_category")}
                out.append(config(ro, key, name, ro_extra))
            out.append((f"{disc_prefix}/{comp}/{node}/{key}/config", ""))  # control variant
    return out


# --------------------------------------------------------------------------
# Publisher
# --------------------------------------------------------------------------

class MqttPublisher:
    def __init__(self, ctl, host: str, port: int = 1883, *, user: str | None = None,
                 password: str | None = None, tls: bool = False, prefix: str = "estyma",
                 device_id: str = "boiler", disc_prefix: str = "homeassistant",
                 stale: float = 300, kwh_per_kg: float | None = None, interval: float = 2,
                 client_id: str | None = None, fuel=None, writes: bool = False) -> None:
        import paho.mqtt.client as mqtt   # optional dependency, only when configured

        self.ctl = ctl
        self.host, self.port = host, port
        self.base = f"{prefix.strip('/')}/{device_id}"
        self.device_id = device_id
        self.disc_prefix = disc_prefix.strip("/")
        self.stale = stale
        self.interval = interval
        self.builder = StateBuilder(kwh_per_kg, fuel)
        self.connected = False
        self._online: bool | None = None
        self._seen_live = False          # a fresh report was seen since start
        self._published_report: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.writes = writes
        self._cmds: dict = {}            # key -> value waiting for the rate limit
        self._cmd_lock = threading.Lock()
        self._disc_sig = None            # controls + limits of the last discovery

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=client_id or f"estyma-local-{device_id}")
        if user:
            self.client.username_pw_set(user, password or None)
        if tls:
            self.client.tls_set()
        self.client.will_set(f"{self.base}/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # -- MQTT callbacks (network thread) ------------------------------------

    def _on_connect(self, client, userdata, flags, reason, properties=None) -> None:
        if reason.is_failure:
            print(f"mqtt: connect to {self.host}:{self.port} refused: {reason}", flush=True)
            return
        self.connected = True
        print(f"mqtt: connected to {self.host}:{self.port}, topics {self.base}/...", flush=True)
        client.subscribe(f"{self.disc_prefix}/status")
        if self.writes:
            client.subscribe(f"{self.base}/set/+")
        self.publish_discovery()
        with self._lock:
            self._online = None          # resend availability and state
            self._published_report = None
        self.tick()

    def _on_disconnect(self, client, userdata, flags, reason, properties=None) -> None:
        if self.connected:
            print(f"mqtt: disconnected ({reason}), reconnecting", flush=True)
        self.connected = False

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic.startswith(f"{self.base}/set/"):
            key = msg.topic[len(self.base) + 5:]
            if msg.retain:
                # A command left retained on the broker would otherwise be
                # replayed on every reconnect.
                print(f"mqtt: ignored retained command {msg.topic}", flush=True)
                return
            try:
                self.command(key, msg.payload.decode("utf-8", "replace"))
            except Exception as exc:   # never let a bad message kill the network thread
                print(f"mqtt: command error: {type(exc).__name__}: {exc}", flush=True)
            return
        if msg.topic == f"{self.disc_prefix}/status" and msg.payload == b"online":
            # Home Assistant restarted: announce again and refresh the state.
            self.publish_discovery()
            with self._lock:
                self._online = None
                self._published_report = None
            self.tick()

    # -- publishing ---------------------------------------------------------

    def _pub(self, topic: str, payload, retain: bool = True) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, qos=1, retain=retain)

    def controls(self) -> dict:
        """key -> discovery limits of the settings Home Assistant may change now."""
        if not self.writes:
            return {}
        out = {}
        for key in CONTROLS:
            w = CTL.WRITABLE[key]
            if self.ctl._lock_reason(w, alarm=False) is None:   # alarms are checked per command
                out[key] = control_limits(self.ctl, key)
        return out

    def publish_discovery(self, controls: dict | None = None) -> None:
        controls = self.controls() if controls is None else controls
        self._disc_sig = json.dumps(controls, sort_keys=True)
        for topic, payload in discovery(self.device_id, self.base, self.disc_prefix, controls):
            self._pub(topic, payload)

    # -- commands -----------------------------------------------------------

    def parse_command(self, key: str, payload: str):
        """Command payload -> value for the controller. Raises ValueError."""
        w = CTL.WRITABLE[key]
        payload = payload.strip()
        if w.kind == "mask":
            CTL.parse_timer_text(payload)          # check it here, for a clear message
            return payload
        if w.kind == "bool":
            p = payload.upper()
            if p in ("ON", "1", "TRUE"):
                return 1
            if p in ("OFF", "0", "FALSE"):
                return 0
            raise ValueError("payload must be ON or OFF")
        if w.options:
            for raw, names in w.options.items():
                if payload.lower() in (n.lower() for n in names) or payload == str(raw):
                    return raw
            raise ValueError(f"unknown option {payload!r}")
        try:
            v = float(payload)
        except ValueError:
            raise ValueError("payload must be a number") from None
        return int(v) if v.is_integer() and w.scale == 1 else v

    def command(self, key: str, payload: str) -> None:
        """One message on ``<p>/set/<key>``: check it and hand it to the controller."""
        allowed = self.controls()
        if key not in allowed:
            reason = ("not a setting that can be changed over MQTT" if key not in CONTROLS
                      else self.ctl._lock_reason(CTL.WRITABLE[key], alarm=False)
                      or "not allowed")
            return self._result(key, payload, False, "rejected", reason)
        if (CONTROLS[key][0] == "select"
                and payload.strip() not in allowed[key].get("options", ())):
            return self._result(key, payload, False, "rejected", f"unknown option {payload.strip()!r}")
        try:
            value = self.parse_command(key, payload)
        except ValueError as exc:
            return self._result(key, payload, False, "rejected", str(exc))
        print(f"mqtt: command {key}={payload.strip()}", flush=True)
        with self._cmd_lock:
            self._cmds[key] = value    # a newer command replaces one waiting for the rate limit
        self.apply_commands()

    def apply_commands(self) -> None:
        """Send waiting commands to the controller's write queue. Commands
        refused only by the rate limit wait and are tried again on the next
        tick, so a burst from an automation ends with the latest value."""
        with self._cmd_lock:
            cmds, self._cmds = self._cmds, {}
            timer = {k: cmds.pop(k) for k in list(cmds) if k in CTL.TIMER_KEYS}
            jobs = [(k, (lambda k=k, v=v: self.ctl.request_write(k, v, source="mqtt")), v)
                    for k, v in cmds.items()]
            if timer:
                jobs.append((",".join(timer), lambda: self.ctl.request_timer(timer, source="mqtt"),
                             timer))
            for key, job, value in jobs:
                try:
                    res = job()
                except CTL.TooFast:
                    for k, v in (value.items() if isinstance(value, dict) else [(key, value)]):
                        self._cmds.setdefault(k, v)
                    continue
                except CTL.WriteError as exc:
                    self._result(key, value, False, "rejected", str(exc))
                    continue
                self._result(key, value, True, res.get("status"), res.get("message"))

    def _result(self, key, value, ok: bool, status, message) -> None:
        if not ok:
            print(f"mqtt: command {key} rejected: {message}", flush=True)
        if self.connected:
            self._pub(f"{self.base}/write", {"key": key, "value": value, "ok": ok, "status": status,
                                            "message": message,
                                            "time": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                      retain=False)

    def tick(self, now: float | None = None) -> None:
        """Publish availability changes and the state after each new report."""
        if not self.connected:
            return
        now = time.time() if now is None else now
        last = self.ctl.last_report
        online = last is not None and now - last <= self.stale
        with self._lock:
            if online != self._online:
                self._online = online
                self._pub(f"{self.base}/availability", "online" if online else "offline")
                if online:
                    print("mqtt: controller reporting, online", flush=True)
                elif last is None or not self._seen_live:
                    # Right after a start the saved last report is from before
                    # the restart; the first new one switches to online.
                    print("mqtt: offline until the controller's next report", flush=True)
                else:
                    print(f"mqtt: no report for {now - last:.0f} s, offline", flush=True)
                self._seen_live = self._seen_live or online
            if last is None or last == self._published_report:
                return
            self._published_report = last
        if self.writes:
            # The controller's min/max can change (and arrive after start).
            controls = self.controls()
            if json.dumps(controls, sort_keys=True) != self._disc_sig:
                self.publish_discovery(controls)
        state, alarm = self.builder.build(self.ctl)
        self._pub(f"{self.base}/state", state)
        self._pub(f"{self.base}/alarm", alarm)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if self._cmds:
                    self.apply_commands()
                self.tick()
            except Exception as exc:   # never let the publisher die silently
                print(f"mqtt: publish error: {type(exc).__name__}: {exc}", flush=True)

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()
        threading.Thread(target=self._run, name="mqtt-publisher", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.connected:
            info = self.client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
            try:
                info.wait_for_publish(2)
            except Exception:
                pass
        self.connected = False
        self.client.disconnect()
        self.client.loop_stop()


def from_env(ctl, env=os.environ, fuel=None) -> MqttPublisher | None:
    """A started publisher when MQTT_HOST is set, else None."""
    host = (env.get("MQTT_HOST") or "").strip()
    if not host:
        return None
    tls = (env.get("MQTT_TLS") or "").strip().lower() in ("1", "true", "yes", "on")
    kwh = (env.get("FUEL_KWH_PER_KG") or "").strip()
    pub = MqttPublisher(
        ctl, host, int(env.get("MQTT_PORT") or (8883 if tls else 1883)),
        user=env.get("MQTT_USER") or None, password=env.get("MQTT_PASSWORD") or None, tls=tls,
        prefix=env.get("MQTT_PREFIX") or "estyma", device_id=env.get("MQTT_DEVICE_ID") or "boiler",
        disc_prefix=env.get("HA_DISCOVERY_PREFIX") or "homeassistant",
        stale=float(env.get("MQTT_STALE_SECONDS") or 300),
        kwh_per_kg=float(kwh) if kwh else None, fuel=fuel,
        writes=(env.get("MQTT_WRITES") or "").strip().lower() in ("1", "true", "yes", "on"))
    pub.start()
    return pub

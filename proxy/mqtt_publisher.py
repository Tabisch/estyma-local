"""Read-only MQTT publisher with Home Assistant discovery.

Publishes what the server knows about the controller (the register state of
``controller.Controller``) to an MQTT broker, so Home Assistant (or anything
else that speaks MQTT) can show it. Nothing is written to the controller:
there are no command topics yet.

Topics (``<p>`` = ``MQTT_PREFIX``/``MQTT_DEVICE_ID``, default ``estyma/boiler``):

* ``<p>/state``        one JSON object with every value (retained), unknown
                       values are ``null``
* ``<p>/alarm``        the newest alarm slot as JSON (retained)
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
  MQTT_STALE_SECONDS   seconds without a report until offline (default 90)
  FUEL_KWH_PER_KG      calorific value for the energy sensor; default: the
                       controller's own setting (8850) if read, else 4.8
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import controller as C
import monitor as M
import protocol as P
import registers as R

REPO_URL = "https://github.com/Tabisch/estyma-local"
DEFAULT_KWH_PER_KG = 4.8   # typical wood pellets
ALARM_TEXT = {140: "missing fuel / ignition failure"}

_LIVE = {k: (reg, idx, how, lo, hi) for k, _, reg, idx, how, _, lo, hi in M.LIVE}

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
    ("sensor", "energy", "Fuel energy", {"device_class": "energy", "unit_of_measurement": "kWh",
                                         "state_class": "total_increasing",
                                         "suggested_display_precision": 0}),
    ("binary_sensor", "boiler_on", "Boiler on", {"device_class": "power"}),
    ("binary_sensor", "boiler_pump", "Boiler pump", {"device_class": "running", "icon": "mdi:pump"}),
    ("binary_sensor", "circuit1_pump", "Heating circuit 1 pump", {"device_class": "running",
                                                                  "icon": "mdi:pump"}),
    ("binary_sensor", "alarm", "Alarm", {"device_class": "problem"}),
    ("sensor", "alarm_code", "Alarm code", {"icon": "mdi:alert-circle-outline"}),
    ("sensor", "lambda_pwm", "Lambda PWM", dict(_DIAG, state_class="measurement",
                                                enabled_by_default=False)),
    ("sensor", "last_report", "Last report", dict(_DIAG, device_class="timestamp")),
    ("sensor", "mode", "Server mode", dict(_DIAG, icon="mdi:server-network")),
]


def _onoff(v):
    return None if v is None else ("ON" if v else "OFF")


class StateBuilder:
    """Controller registers -> the JSON published on ``<p>/state``.

    Keeps the fuel counter monotonic: a lower reading, or a jump larger than
    the boiler can burn, is a corrupted read and keeps the last good value,
    so Home Assistant's energy statistics never see a fake reset.
    """

    MAX_KG_PER_HOUR = 20.0

    def __init__(self, kwh_per_kg: float | None = None) -> None:
        self.kwh_per_kg = kwh_per_kg
        self._fuel: tuple | None = None   # (ts, kg) last accepted

    def _live(self, regs: dict, key: str):
        reg, idx, how, lo, hi = _LIVE[key]
        v = regs.get((reg, idx))
        return None if v is None else M.decode(v[1], how, lo, hi)

    def _fuel_total(self, regs: dict):
        v = regs.get((8484, 1))
        kg = M.decode(v[1], "d10", 0, 2**31 - 1) if v else None
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
        if self.kwh_per_kg:
            return self.kwh_per_kg
        cur = ctl.current("calorific_value")
        if cur is not None and 20 <= cur <= 100:
            return cur / 10
        return DEFAULT_KWH_PER_KG

    def alarm(self, ctl) -> dict:
        slot = ctl.alarm_slot()
        out = {"active": bool(ctl.alarm_active()), "slot": slot, "code": None, "text": None,
               "state": None, "start": None, "end": None}
        if slot is None:
            return out
        raw = ctl.raw(P.ALARM_CODE_BASE + slot)
        if raw is not None and raw >= 0:
            code, state = raw & 0xFF, (raw >> 16) & 0xFFFF
            out.update(code=code, text=ALARM_TEXT.get(code),
                       state=M.ALARM_STATES.get(state, f"state {state}"))
        # Controller clock: local wall time encoded as Unix time, shown as is.
        for key, base in (("start", P.ALARM_START_BASE), ("end", P.ALARM_END_BASE)):
            t = ctl.raw(base + slot)
            if t == 0 and key == "end":
                out[key] = 0
            elif t and M.MIN_TIME <= t:
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
        st = {k: self._live(regs, k) for k in
              ("boiler", "boiler_calc", "buffer_top", "buffer_bottom", "circuit1", "outside",
               "flue", "o2", "power", "lambda_pwm")}
        bs = self._live(regs, "burner_state")
        st["burner_state"] = (None if bs is None else
                              R.VALUES[8199].get(int(bs), (f"state {int(bs)}",))[0])
        st["burner_state_code"] = None if bs is None else int(bs)
        st["boiler_target"] = target if target is not None and 0 < target <= 120 else None
        st["boiler_on"] = _onoff(on if on in (0, 1) else None)
        pump = self._live(regs, "boiler_pump")
        st["boiler_pump"] = _onoff(None if pump is None else int(pump))
        c1 = regs.get((8857, 1))
        c1v = C.low8(c1[1]) if c1 else None
        st["circuit1_pump"] = _onoff(c1v if c1v in (0, 1) else None)
        fuel = self._fuel_total(regs)
        st["fuel_total"] = fuel
        kwh = self.calorific_value(ctl)
        st["energy"] = round(fuel * kwh, 1) if fuel is not None else None
        st["kwh_per_kg"] = kwh
        st["alarm"] = "ON" if alarm["active"] else "OFF"
        st["alarm_code"] = alarm["code"] if alarm["active"] else 0
        st["last_report"] = (datetime.fromtimestamp(last, timezone.utc).isoformat(timespec="seconds")
                             if last else None)
        st["mode"] = mode
        return st, alarm


def discovery(device_id: str, base: str, disc_prefix: str) -> list[tuple[str, str]]:
    """Retained (topic, payload) pairs announcing the device to Home Assistant."""
    node = f"estyma_{device_id}"
    device = {"identifiers": [node], "name": "Estyma boiler", "manufacturer": "Estyma",
              "model": "Boiler controller (local server)"}
    origin = {"name": "estyma-local", "url": REPO_URL}
    out = []
    for comp, key, name, extra in ENTITIES:
        cfg = {"name": name, "unique_id": f"{node}_{key}", "state_topic": f"{base}/state",
               "value_template": "{{ value_json.%s }}" % key,
               "availability_topic": f"{base}/availability", "device": device, "origin": origin}
        if key == "alarm":
            cfg["json_attributes_topic"] = f"{base}/alarm"
        cfg.update(extra)
        out.append((f"{disc_prefix}/{comp}/{node}/{key}/config", json.dumps(cfg, ensure_ascii=False)))
    return out


# --------------------------------------------------------------------------
# Publisher
# --------------------------------------------------------------------------

class MqttPublisher:
    def __init__(self, ctl, host: str, port: int = 1883, *, user: str | None = None,
                 password: str | None = None, tls: bool = False, prefix: str = "estyma",
                 device_id: str = "boiler", disc_prefix: str = "homeassistant",
                 stale: float = 90, kwh_per_kg: float | None = None, interval: float = 2,
                 client_id: str | None = None) -> None:
        import paho.mqtt.client as mqtt   # optional dependency, only when configured

        self.ctl = ctl
        self.host, self.port = host, port
        self.base = f"{prefix.strip('/')}/{device_id}"
        self.device_id = device_id
        self.disc_prefix = disc_prefix.strip("/")
        self.stale = stale
        self.interval = interval
        self.builder = StateBuilder(kwh_per_kg)
        self.connected = False
        self._online: bool | None = None
        self._seen_live = False          # a fresh report was seen since start
        self._published_report: float | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

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

    def publish_discovery(self) -> None:
        for topic, payload in discovery(self.device_id, self.base, self.disc_prefix):
            self._pub(topic, payload)

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
        state, alarm = self.builder.build(self.ctl)
        self._pub(f"{self.base}/state", state)
        self._pub(f"{self.base}/alarm", alarm)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
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


def from_env(ctl, env=os.environ) -> MqttPublisher | None:
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
        stale=float(env.get("MQTT_STALE_SECONDS") or 90),
        kwh_per_kg=float(kwh) if kwh else None)
    pub.start()
    return pub

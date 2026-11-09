"""MQTT publisher: state decoding, discovery, and a run against a real broker."""

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from fixtures import report

from controller import Controller
import mqtt_publisher as MP

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_ctl(tmp, clock=None, mode="proxy"):
    return Controller(mode, tmp, state_file=f"{tmp}/state.json", clock=clock or Clock())


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.ctl = make_ctl(self.tmp, self.clock)

    def test_decoded_values(self):
        self.ctl.observe_report(report(r8480_1=950, r16389_1=63, r8823_1=257, r8857_1=256,
                                       r8473_1=256 + 120, r8543_1=65))
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["boiler"], 70.5)
        self.assertEqual(st["buffer_top"], 72.0)
        self.assertEqual(st["circuit1"], 51.1)
        self.assertEqual(st["outside"], 6.5)
        self.assertEqual(st["flue"], 120)
        self.assertEqual(st["o2"], 9.5)
        self.assertEqual(st["power"], 63)
        self.assertEqual(st["burner_state"], "modulation")
        self.assertEqual(st["burner_state_code"], 5)
        self.assertEqual(st["boiler_target"], 60)
        self.assertEqual(st["boiler_on"], "ON")
        self.assertEqual(st["boiler_pump"], "ON")
        self.assertEqual(st["circuit1_pump"], "OFF")
        self.assertEqual(st["fuel_total"], 54157.0)
        self.assertEqual(st["kwh_per_kg"], 4.8)
        self.assertEqual(st["energy"], round(54157.0 * 4.8, 1))
        self.assertEqual(st["alarm"], "OFF")
        self.assertEqual(st["mode"], "proxy")
        self.assertTrue(st["last_report"].startswith("2027-"))
        # Every entity has a field in the state, so no template ever fails.
        for _, key, _, _ in MP.ENTITIES:
            self.assertIn(key, st)
        json.dumps(st)
        json.dumps(alarm)

    def test_unknown_values_are_null(self):
        st, _ = MP.StateBuilder().build(self.ctl)
        self.assertIsNone(st["boiler"])
        self.assertIsNone(st["energy"])
        self.assertIsNone(st["boiler_on"])
        self.assertIsNone(st["last_report"])

    def test_glitches_dropped(self):
        # 8193 with garbage in the upper bits is out of range -> null.
        self.ctl.observe_report(report(r8193_1=705 + (5 << 16), r8199_1=0x3405))
        st, _ = MP.StateBuilder().build(self.ctl)
        self.assertIsNone(st["boiler"])
        self.assertIsNone(st["burner_state"])

    def test_single_glitch_keeps_last_value(self):
        b = MP.StateBuilder()
        self.ctl.observe_report(report())
        self.assertEqual(b.build(self.ctl)[0]["boiler_on"], "ON")
        self.clock.t += 20
        self.ctl.observe_report(report(r8197_1=0x6020001, r8193_1=705 + (5 << 16)))
        st = b.build(self.ctl)[0]
        self.assertEqual(st["boiler_on"], "ON")
        self.assertEqual(st["boiler"], 70.5)
        # Still invalid after HOLD_SECONDS: unknown.
        self.clock.t += b.HOLD_SECONDS + 20
        self.ctl.observe_report(report(r8197_1=0x1))
        self.assertIsNone(b.build(self.ctl)[0]["boiler_on"])

    def test_fuel_counter_monotonic(self):
        b = MP.StateBuilder(kwh_per_kg=5)
        self.ctl.observe_report(report())
        self.assertEqual(b.build(self.ctl)[0]["fuel_total"], 54157.0)
        self.clock.t += 20
        self.ctl.observe_report(report(r8484_1=541000))      # went down: glitch
        self.assertEqual(b.build(self.ctl)[0]["fuel_total"], 54157.0)
        self.clock.t += 20
        self.ctl.observe_report(report(r8484_1=9_541_570))   # huge jump: glitch
        self.assertEqual(b.build(self.ctl)[0]["fuel_total"], 54157.0)
        self.clock.t += 20
        self.ctl.observe_report(report(r8484_1=541580))      # +1 kg: fine
        st = b.build(self.ctl)[0]
        self.assertEqual(st["fuel_total"], 54158.0)
        self.assertEqual(st["energy"], 270790.0)

    def test_calorific_value_from_controller(self):
        self.ctl.observe_report(report(r8850_1=256 + 50))
        self.assertEqual(MP.StateBuilder().build(self.ctl)[0]["kwh_per_kg"], 5.0)
        self.assertEqual(MP.StateBuilder(4.2).build(self.ctl)[0]["kwh_per_kg"], 4.2)

    def test_alarm(self):
        # Counter 268 -> n = 12 -> slot 11; code 140, state 3 (active), no end.
        self.ctl.observe_report(report(r16411_1=(3 << 16) | 140, r16451_1=0))
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["alarm"], "ON")
        self.assertEqual(st["alarm_code"], 140)
        self.assertEqual(alarm["slot"], 11)
        self.assertEqual(alarm["state"], "active")
        self.assertEqual(alarm["text"], "missing fuel / ignition failure")
        self.assertEqual(alarm["end"], 0)
        self.assertTrue(alarm["start"].startswith("2026-"))

    def test_cleared_alarm(self):
        self.ctl.observe_report(report())   # fixture: code 140 state 0, ended
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["alarm"], "OFF")
        self.assertEqual(st["alarm_code"], 0)
        self.assertEqual(alarm["code"], 140)
        self.assertEqual(alarm["state"], "cleared")


class DiscoveryTest(unittest.TestCase):
    def test_configs(self):
        cfgs = MP.discovery("boiler", "estyma/boiler", "homeassistant")
        self.assertEqual(len(cfgs), len(MP.ENTITIES))
        ids = set()
        for topic, payload in cfgs:
            cfg = json.loads(payload)
            self.assertTrue(topic.startswith("homeassistant/"))
            self.assertTrue(topic.endswith("/config"))
            self.assertEqual(cfg["state_topic"], "estyma/boiler/state")
            self.assertEqual(cfg["availability_topic"], "estyma/boiler/availability")
            self.assertEqual(cfg["device"]["identifiers"], ["estyma_boiler"])
            ids.add(cfg["unique_id"])
        self.assertEqual(len(ids), len(cfgs))
        energy = json.loads(dict(cfgs)["homeassistant/sensor/estyma_boiler/energy/config"])
        self.assertEqual(energy["device_class"], "energy")
        self.assertEqual(energy["state_class"], "total_increasing")
        self.assertEqual(energy["unit_of_measurement"], "kWh")
        alarm = json.loads(dict(cfgs)["homeassistant/binary_sensor/estyma_boiler/alarm/config"])
        self.assertEqual(alarm["json_attributes_topic"], "estyma/boiler/alarm")
        # Read-only: no command topics.
        self.assertFalse(any("command_topic" in p for _, p in cfgs))

    def test_from_env_off_without_host(self):
        self.assertIsNone(MP.from_env(None, {}))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(mqtt and shutil.which("mosquitto"), "needs paho-mqtt and mosquitto")
class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.port = _free_port()
        conf = f"{self.tmp}/m.conf"
        with open(conf, "w") as fh:
            fh.write(f"listener {self.port} 127.0.0.1\nallow_anonymous true\n")
        self.broker = subprocess.Popen(["mosquitto", "-c", conf], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        self.addCleanup(self.broker.wait)
        self.addCleanup(self.broker.terminate)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        self.msgs = {}
        self.got = threading.Condition()
        self.sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-sub")
        self.sub.on_message = self._on_msg
        self.sub.connect("127.0.0.1", self.port)
        self.sub.subscribe("#")
        self.sub.loop_start()
        self.addCleanup(self.sub.loop_stop)
        self.addCleanup(self.sub.disconnect)

    def _on_msg(self, c, u, msg):
        with self.got:
            self.msgs[msg.topic] = msg.payload.decode()
            self.got.notify_all()

    def wait_for(self, pred, timeout=5):
        with self.got:
            self.assertTrue(self.got.wait_for(lambda: pred(self.msgs), timeout), self.msgs.keys())

    def test_publish(self):
        ctl = make_ctl(self.tmp, time.time)
        pub = MP.MqttPublisher(ctl, "127.0.0.1", self.port, interval=0.1, stale=1)
        pub.start()
        self.addCleanup(pub.stop)
        n = len(MP.ENTITIES)
        self.wait_for(lambda m: sum(t.startswith("homeassistant/") for t in m) == n
                      and m.get("estyma/boiler/availability") == "offline")
        self.assertNotIn("estyma/boiler/state", self.msgs)

        ctl.observe_report(report())
        self.wait_for(lambda m: m.get("estyma/boiler/availability") == "online"
                      and "estyma/boiler/state" in m and "estyma/boiler/alarm" in m)
        st = json.loads(self.msgs["estyma/boiler/state"])
        self.assertEqual(st["boiler"], 70.5)
        self.assertEqual(json.loads(self.msgs["estyma/boiler/alarm"])["state"], "cleared")

        # No report for longer than MQTT_STALE_SECONDS -> offline.
        self.wait_for(lambda m: m.get("estyma/boiler/availability") == "offline", timeout=4)

        # Home Assistant restart: discovery is sent again.
        with self.got:
            for t in [t for t in self.msgs if t.startswith("homeassistant/")]:
                del self.msgs[t]
        self.sub.publish("homeassistant/status", "online")
        self.wait_for(lambda m: sum(t.startswith("homeassistant/sensor/") for t in m) > 5)


if __name__ == "__main__":
    unittest.main()

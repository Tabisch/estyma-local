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

from estyma_local.controller import Controller
from estyma_local import mqtt_publisher as MP

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

    def test_burner_state_sustain(self):
        self.ctl.observe_report(report(r8199_1=256 + 8))
        st, _ = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["burner_state"], "sustain")
        self.assertEqual(st["burner_state_code"], 8)

    def test_burner_state_burn_out(self):
        self.ctl.observe_report(report(r8199_1=256 + 6))
        st, _ = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["burner_state"], "burn-out")
        self.assertEqual(st["burner_state_code"], 6)

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
        self.assertEqual(alarm["text"], "missing fuel / no fire")
        self.assertEqual(alarm["end"], 0)
        self.assertTrue(alarm["start"].startswith("2026-"))
        self.assertEqual(st["alarm_text"], "missing fuel / no fire")

    def test_start_failure_alarm(self):
        # Code 139 "Fehler beim Starten" as seen on the controller display.
        self.ctl.observe_report(report(r16411_1=(3 << 16) | 139, r16451_1=0))
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["alarm"], "ON")
        self.assertEqual(st["alarm_code"], 139)
        self.assertEqual(st["alarm_text"], "start failure")
        self.assertEqual(alarm["text"], "start failure")

    def test_alarm_texts_match_monitor_page(self):
        import pathlib, re
        from estyma_local import catalog as CAT, monitor as M
        html = (pathlib.Path(M.__file__).parent / "pages" / "monitor.html").read_text()
        for code in CAT.ALARM_TEXT:
            self.assertEqual(len(re.findall(r'"alarm\.%d":' % code, html)), 2, code)

    def test_unknown_alarm_code(self):
        self.ctl.observe_report(report(r16411_1=(3 << 16) | 77, r16451_1=0))
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["alarm_text"], "code 77")
        self.assertIsNone(alarm["text"])

    def test_cleared_alarm(self):
        self.ctl.observe_report(report())   # fixture: code 140 state 0, ended
        st, alarm = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["alarm"], "OFF")
        self.assertEqual(st["alarm_code"], 0)
        self.assertEqual(st["alarm_text"], "none")
        self.assertEqual(alarm["code"], 140)
        self.assertEqual(alarm["state"], "cleared")


class DiscoveryTest(unittest.TestCase):
    def test_configs(self):
        all_cfgs = MP.discovery("boiler", "estyma/boiler", "homeassistant")
        cfgs = [(t, p) for t, p in all_cfgs if p]
        ro = [k for k in MP.CONTROLS if k not in MP._IN_ENTITIES]
        self.assertEqual(len(cfgs), len(MP.ENTITIES) + len(ro))
        # the command variants are cleared
        self.assertEqual(len(all_cfgs) - len(cfgs), len(MP.CONTROLS))
        self.assertIn(("homeassistant/number/estyma_boiler/boiler_target/config", ""), all_cfgs)
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

    def test_control_configs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ctl = Controller("hybrid", tmp, state_file=f"{tmp}/state.json", clock=Clock(),
                         writes_enabled=True)
        ctl.observe_report(report(r8242_1=210, r8242_3=150, r8242_4=300, r8292_1=259))
        pub = MP.MqttPublisher(ctl, "127.0.0.1", writes=True)
        controls = pub.controls()
        self.assertNotIn("boiler_on", controls)           # ALLOW_POWER_WRITES=0
        self.assertIn("co1_timer_mon", controls)
        self.assertEqual(controls["room_comfort_1"], {"min": 15.0, "max": 30.0, "step": 0.1})
        self.assertEqual(controls["boiler_target"], {"min": 60, "max": 80, "step": 1})
        self.assertEqual(controls["circuit1_program"]["options"], ["off", "economical", "comfortable"])
        cfgs = dict(MP.discovery("boiler", "estyma/boiler", "homeassistant", controls))
        num = json.loads(cfgs["homeassistant/number/estyma_boiler/boiler_target/config"])
        self.assertEqual(num["command_topic"], "estyma/boiler/set/boiler_target")
        self.assertEqual((num["min"], num["max"], num["mode"]), (60, 80, "box"))
        self.assertEqual(cfgs["homeassistant/sensor/estyma_boiler/boiler_target/config"], "")
        sel = json.loads(cfgs["homeassistant/select/estyma_boiler/circuit1_program/config"])
        self.assertNotIn("unit_of_measurement", sel)
        txt = json.loads(cfgs["homeassistant/text/estyma_boiler/co1_timer_mon/config"])
        self.assertEqual(txt["entity_category"], "config")
        self.assertRegex("06-22", txt["pattern"])
        self.assertRegex("06-09, 16-22", txt["pattern"])
        self.assertRegex("", txt["pattern"])
        self.assertNotRegex("6 to 22", txt["pattern"])
        # on/off still a binary sensor
        self.assertTrue(cfgs["homeassistant/binary_sensor/estyma_boiler/boiler_on/config"])
        self.assertEqual(cfgs["homeassistant/switch/estyma_boiler/boiler_on/config"], "")
        # read-only without MQTT_WRITES
        self.assertEqual(MP.MqttPublisher(ctl, "127.0.0.1").controls(), {})

    def test_from_env_off_without_host(self):
        self.assertIsNone(MP.from_env(None, {}))


class CommandTest(unittest.TestCase):
    """Commands without a broker: the publisher's results are collected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.ctl = Controller("hybrid", self.tmp, state_file=f"{self.tmp}/state.json",
                              clock=self.clock, writes_enabled=True, allow_power_writes=True)
        regs = {f"r{8486 + 4 * d}_1": 0x3FFFC0 for d in range(7)}
        self.ctl.observe_report(report(r8292_1=259, r8242_1=210, **regs))
        self.pub = MP.MqttPublisher(self.ctl, "127.0.0.1", writes=True)
        self.results = []
        self.pub._result = lambda *a: self.results.append(a)

    def pending(self):
        return {k: p.value for k, p in self.ctl.pending.items()}

    def test_number(self):
        self.pub.command("boiler_target", "62")
        self.assertEqual(self.pending(), {"boiler_target": 62})
        self.assertTrue(self.results[-1][2])
        self.assertEqual(self.pending()["boiler_target"], 62)
        self.pub.command("room_comfort_1", "21.5")
        self.assertEqual(self.pending()["room_comfort_1"], 215)
        self.assertEqual(self.ctl.pending["boiler_target"].source, "mqtt")

    def test_out_of_range_rejected(self):
        self.pub.command("boiler_target", "95")
        self.assertEqual(self.pending(), {})
        key, _, ok, status, msg = self.results[-1]
        self.assertFalse(ok)
        self.assertIn("between", msg)

    def test_switch_and_select(self):
        self.pub.command("boiler_on", "OFF")
        self.pub.command("circuit1_program", "economical")
        self.assertEqual(self.pending(), {"boiler_on": 0, "circuit1_program": 1})
        self.pub.command("circuit1_program", "temporary")     # never tested: not offered
        self.assertFalse(self.results[-1][2])

    def test_timer_day(self):
        self.pub.command("co1_timer_sat", "08-20")
        self.assertEqual(self.pending()["co1_timer_sat"], 0xFFF00)
        self.assertEqual(len(self.pending()), 7)                # sent with the other days
        self.pub.command("co1_timer_sun", "nonsense")
        self.assertFalse(self.results[-1][2])

    def test_rate_limited_command_waits_and_keeps_latest(self):
        self.pub.command("boiler_target", "62")
        self.pub.command("boiler_target", "63")                 # within WRITE_MIN_INTERVAL
        self.pub.command("boiler_target", "64")
        self.assertEqual(self.pending(), {"boiler_target": 62})
        self.assertEqual(self.pub._cmds, {"boiler_target": 64})
        self.clock.t += 11
        self.pub.apply_commands()
        self.assertEqual(self.pending(), {"boiler_target": 64})
        self.assertEqual(self.pub._cmds, {})

    def test_two_timer_days_quickly_are_merged(self):
        self.pub.command("co1_timer_mon", "")
        self.pub.command("co1_timer_tue", "")
        self.clock.t += 11
        self.pub.apply_commands()
        self.assertEqual((self.pending()["co1_timer_mon"], self.pending()["co1_timer_tue"]), (0, 0))

    def test_unknown_or_locked_key(self):
        self.pub.command("hyst_on", "5")
        self.assertIn("can be changed over MQTT", self.results[-1][4])
        ro = MP.MqttPublisher(self.ctl, "127.0.0.1", writes=False)
        ro._result = lambda *a: self.results.append(a)
        ro.command("boiler_target", "62")
        self.assertEqual(self.pending(), {})

    def test_state_has_settings(self):
        st, _ = MP.StateBuilder().build(self.ctl)
        self.assertEqual(st["circuit1_program"], "comfortable")
        self.assertEqual(st["room_comfort_1"], 21.0)
        self.assertEqual(st["co1_timer_mon"], "06-22")


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
        n = len(MP.ENTITIES) + len([k for k in MP.CONTROLS if k not in MP._IN_ENTITIES])
        self.wait_for(lambda m: sum(t.startswith("homeassistant/") and bool(p)
                                    for t, p in m.items()) == n
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

    def test_commands(self):
        ctl = Controller("hybrid", self.tmp, state_file=f"{self.tmp}/state.json",
                         writes_enabled=True)
        ctl.observe_report(report())
        # A command left retained on the broker is not applied on connect.
        self.sub.publish("estyma/boiler/set/boiler_target", "70", retain=True).wait_for_publish(2)
        self.addCleanup(lambda: self.sub.publish("estyma/boiler/set/boiler_target", "", retain=True))
        pub = MP.MqttPublisher(ctl, "127.0.0.1", self.port, interval=0.1, writes=True)
        pub.start()
        self.addCleanup(pub.stop)
        self.wait_for(lambda m: m.get("homeassistant/number/estyma_boiler/boiler_target/config"))
        cfg = json.loads(self.msgs["homeassistant/number/estyma_boiler/boiler_target/config"])
        time.sleep(0.5)
        self.assertEqual(ctl.pending, {})
        self.sub.publish(cfg["command_topic"], "63")
        self.wait_for(lambda m: "estyma/boiler/write" in m)
        res = json.loads(self.msgs["estyma/boiler/write"])
        self.assertEqual((res["key"], res["ok"], res["status"]), ("boiler_target", True, "pending"))
        self.assertEqual(ctl.pending["boiler_target"].value, 63)
        self.assertEqual(ctl.pending["boiler_target"].source, "mqtt")


if __name__ == "__main__":
    unittest.main()

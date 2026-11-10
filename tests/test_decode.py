"""decode.py: the one rule for corrupted reads, used by every view."""

import shutil
import tempfile
import unittest

from fixtures import report

import decode as D
import monitor as M
import mqtt_publisher as MP
from controller import Controller
from registers import RegisterTable


class ValueTest(unittest.TestCase):
    def test_scaled_and_ranged(self):
        self.assertEqual(D.value(8193, 705), 70.5)
        self.assertIsNone(D.value(8193, 705 + (5 << 16)))   # upper-bit garbage -> out of range
        self.assertEqual(D.value(8480, 950), 9.5)
        self.assertIsNone(D.value(8480, 9500))              # 95 % O2

    def test_8bit(self):
        self.assertEqual(D.value(8473, 0x100 + 120), 120)
        self.assertEqual(D.value(8473, 120), 120)
        self.assertIsNone(D.value(8473, 0x6020078))         # garbage above the flag
        self.assertIsNone(D.value(8199, 0x3405))

    def test_setting_ranges_used_without_plausible(self):
        self.assertEqual(D.value(8712, 5), 5)
        self.assertIsNone(D.value(8712, 30))                # hysteresis 0..20

    def test_unknown_register_is_raw(self):
        self.assertEqual(D.value(9999, 12345), 12345)

    def test_last_good(self):
        g = D.LastGood(hold=60)
        self.assertEqual(g.get("k", 5, 0), 5)
        self.assertEqual(g.get("k", None, 30), 5)
        self.assertIsNone(g.get("k", None, 100))

    def test_alarms(self):
        self.assertEqual(D.alarm_code((3 << 16) | 139), (139, "active"))
        self.assertIsNone(D.alarm_code((9 << 16) | 139))    # unknown state
        self.assertEqual(D.alarm_slot(0x100 + 12), 11)
        self.assertEqual(D.alarm_slot(0x100), 19)
        self.assertIsNone(D.alarm_slot(0x2000C))
        self.assertTrue(D.alarm_is_active((3 << 16) | 139, 0))
        self.assertFalse(D.alarm_is_active((3 << 16) | 139, 1_800_000_000))
        self.assertFalse(D.alarm_is_active((0 << 16) | 139, 0))


class ViewsAgreeTest(unittest.TestCase):
    """A corrupted read is dropped by the monitor, nulled (or held) by MQTT and
    flagged on the registers page."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_glitch_everywhere(self):
        bad = 705 + (5 << 16)
        live, _ = M.decode_registers({(8193, 1): bad})
        self.assertNotIn("boiler", live)

        ctl = Controller("proxy", self.tmp, state_file=f"{self.tmp}/s.json")
        ctl.observe_report(report(r8193_1=bad))
        st, _ = MP.StateBuilder().build(ctl)
        self.assertIsNone(st["boiler"])

        table = RegisterTable()
        table.observe(1.0, {(8193, 1): 705})
        table.observe(2.0, {(8193, 1): 705})
        table.observe(3.0, {(8193, 1): bad})
        row = next(r for r in table.snapshot() if r["reg"] == 8193)
        self.assertTrue(row["glitch"])

    def test_normal_change_is_not_a_glitch(self):
        table = RegisterTable()
        table.observe(1.0, {(8193, 1): 705})
        table.observe(2.0, {(8193, 1): 705})
        table.observe(3.0, {(8193, 1): 706})
        row = next(r for r in table.snapshot() if r["reg"] == 8193)
        self.assertTrue(row["pending"])
        self.assertFalse(row["glitch"])


if __name__ == "__main__":
    unittest.main()

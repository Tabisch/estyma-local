"""Fuel consumption per period (fuel_stats.py) and its API / MQTT values."""

import json
import shutil
import tempfile
import unittest
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from fixtures import report

from estyma_local import fuel_stats as F
from estyma_local import mqtt_publisher as MP
from estyma_local.controller import Controller
from estyma_local.history import HistoryStore
from estyma_local.web import start_web

TZ = ZoneInfo("Europe/Warsaw")


def ts(*args) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = HistoryStore(self.dir)
        self.addCleanup(self.store.close)
        self.stats = F.FuelStats(self.store, TZ)

    def counter(self, t, kg):
        self.store.record(t, report(r8484_1=round(kg * 10)))

    def test_days_split_at_local_midnight(self):
        self.counter(ts(2027, 1, 14, 22, 0), 1000)
        self.counter(ts(2027, 1, 14, 23, 59), 1005)
        self.counter(ts(2027, 1, 15, 0, 1), 1006)
        self.counter(ts(2027, 1, 15, 12, 0), 1030)
        now = ts(2027, 1, 15, 13, 0)
        days = self.stats.buckets("day", 3, now)
        self.assertEqual([d["start"] for d in days], ["2027-01-13", "2027-01-14", "2027-01-15"])
        self.assertEqual([d["kg"] for d in days], [None, 5.0, 25.0])
        s = self.stats.summary(now)
        self.assertEqual((s["today"], s["yesterday"], s["month"], s["year"]), (25.0, 5.0, 30.0, 30.0))

    def test_weeks_start_monday_and_months_and_years(self):
        self.counter(ts(2026, 12, 31, 23, 0), 100)   # Thursday, December
        self.counter(ts(2026, 12, 31, 23, 59), 110)
        self.counter(ts(2027, 1, 1, 0, 30), 112)     # same week, new month and year
        self.counter(ts(2027, 1, 3, 23, 30), 112)
        self.counter(ts(2027, 1, 3, 23, 50), 120)    # Sunday
        self.counter(ts(2027, 1, 4, 0, 10), 125)     # Monday
        now = ts(2027, 1, 4, 1)
        self.assertEqual([(w["start"], w["kg"]) for w in self.stats.buckets("week", 2, now)],
                         [("2026-12-28", 20.0), ("2027-01-04", 5.0)])
        self.assertEqual([(m["start"], m["kg"]) for m in self.stats.buckets("month", 2, now)],
                         [("2026-12-01", 10.0), ("2027-01-01", 15.0)])
        self.assertEqual([(y["start"], y["kg"]) for y in self.stats.buckets("year", 2, now)],
                         [("2026-01-01", 10.0), ("2027-01-01", 15.0)])

    def test_single_bad_reads_are_ignored(self):
        t = ts(2027, 1, 15, 8)
        self.counter(t, 1000)
        self.counter(t + 20, 3)          # corrupted low read
        self.counter(t + 40, 1001)
        self.counter(t + 60, 90000)      # impossible jump
        self.counter(t + 80, 1002)
        self.assertEqual(self.stats.summary(t + 100)["today"], 2.0)

    def test_lasting_reset_becomes_new_base(self):
        t = ts(2027, 1, 15, 8)
        self.counter(t, 1000)
        self.counter(t + 20, 1001)
        self.counter(t + 40, 0)                       # counter reset on the controller
        self.counter(t + 40 + F.CONFIRM_S, 1)
        self.counter(t + 60 + F.CONFIRM_S, 2)
        self.assertEqual(self.stats.summary(t + 2000)["today"], 2.0)

    def test_gap_is_spread_and_marked(self):
        self.counter(ts(2027, 1, 14, 12), 1000)
        self.counter(ts(2027, 1, 15, 12), 1040)  # server was down; 40 kg in 24 h is plausible
        self.counter(ts(2027, 1, 15, 13), 1041)
        days = self.stats.buckets("day", 3, ts(2027, 1, 16, 13))
        self.assertEqual([(d["kg"], d["estimated"]) for d in days],
                         [(20.0, True), (21.0, True), (0.0, False)])

    def test_hourly_keyframes_are_no_gap(self):
        self.counter(ts(2027, 1, 14, 23, 30), 1000)
        self.counter(ts(2027, 1, 15, 0, 30), 1000)
        self.counter(ts(2027, 1, 15, 1, 20), 1001)
        days = self.stats.buckets("day", 2, ts(2027, 1, 15, 13))
        self.assertEqual([(d["kg"], d["estimated"]) for d in days], [(0.0, False), (1.0, False)])

    def test_picks_up_new_readings(self):
        t = ts(2027, 1, 15, 8)
        self.counter(t, 1000)
        self.assertEqual(self.stats.summary(t + 1)["today"], 0.0)
        self.counter(t + 20, 1003)
        self.assertEqual(self.stats.summary(t + 30)["today"], 3.0)

    def test_empty_store(self):
        self.assertEqual(self.stats.summary()["today"], None)
        self.assertTrue(all(b["kg"] is None for b in self.stats.buckets("month")))

    def test_mqtt_state_has_today_and_yesterday(self):
        t = ts(2027, 1, 15, 8)
        self.counter(t - 3600 * 9, 990)       # 23:00 the day before
        self.counter(t - 3600 * 9 + 60, 1000)
        self.counter(t - 3600 * 8 - 60, 1000)   # 00:59
        self.counter(t - 3600 * 8 + 60, 1012)
        stats = F.FuelStats(self.store, TZ, clock=lambda: t + 60)
        ctl = Controller("proxy", self.dir, state_file=f"{self.dir}/state.json", clock=lambda: t)
        ctl.observe_report(report(r8484_1=10120))
        st, _ = MP.StateBuilder(fuel=stats).build(ctl)
        self.assertEqual((st["fuel_today"], st["fuel_yesterday"]), (12.0, 10.0))
        keys = {k for _, k, _, _ in MP.ENTITIES}
        self.assertTrue({"fuel_today", "fuel_yesterday"} <= keys)

    def test_api(self):
        self.counter(ts(2027, 1, 15, 8), 1000)
        self.counter(ts(2027, 1, 15, 9), 1002)
        srv = start_web("127.0.0.1", 0, self.dir, fuel=self.stats)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fuel?period=month&count=3") as r:
            out = json.load(r)
        self.assertEqual(out["period"], "month")
        self.assertEqual(len(out["buckets"]), 3)
        self.assertEqual(out["counter"], 1002.0)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fuel?period=hour")
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

"""Cloud fuel statistics import: fetching, storing, merging into the fuel statistics, web API."""

import base64
import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fixtures import report

from estyma_local import cloud_fuel_import as CI
from estyma_local import fuel_stats as F
from estyma_local.history import HistoryStore
from estyma_local.web import start_web

TZ = ZoneInfo("Europe/Warsaw")
ENV = {"ESTYMA_EMAIL": "user@example.com", "ESTYMA_PASSWORD": "secret"}
TODAY = date(2027, 1, 15)


def ts(*args) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


def month_result(year, month, kg_by_day):
    """What EstymaApi.getFuelStatistics returns (parsed, not raw)."""
    from calendar import monthrange
    data = [{"day": str(d), "fuel": kg_by_day.get(d, 0), "price": None, "cost": None, "currency": None}
            for d in range(1, monthrange(year, month)[1] + 1)]
    return {"year": year, "month": month, "data": data}


class FakeApi:
    devices = {"123": {"name": "boiler"}}
    days = {}          # (y, m) -> {day: kg}
    years = [2026, 2027]
    calls = []

    async def initialize(self):
        pass

    async def getDevices(self):
        return self.devices

    async def getStatisticsLimits(self, device):
        return {"fuelStatistics": {"years": self.years, "months": {m: str(m) for m in range(13)}}}

    async def getFuelStatistics(self, device, year, month, validate=True):
        type(self).calls.append((device, year, month))
        return month_result(year, month, self.days.get((year, month), {}))

    async def _logout(self):
        pass


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class ParseTest(unittest.TestCase):
    def test_day_names(self):
        self.assertEqual(CI.day_of("7", 0, 2026, 11), date(2026, 11, 7))
        self.assertEqual(CI.day_of("07.11", 0, 2026, 11), date(2026, 11, 7))
        self.assertEqual(CI.day_of("2026-11-07", 0, 2026, 11), date(2026, 11, 7))
        self.assertEqual(CI.day_of(None, 6, 2026, 11), date(2026, 11, 7))
        self.assertIsNone(CI.day_of("31", 0, 2026, 11))

    def test_month_days_skips_bad_values(self):
        res = {"data": [{"day": "1", "fuel": 30.5}, {"day": "2", "fuel": None},
                        {"day": "3", "fuel": "1,020.0"}, {"day": "4", "fuel": -1}, {"day": "5", "fuel": "12"}]}
        self.assertEqual(CI.month_days(res, 2026, 11), {date(2026, 11, 1): 30.5, date(2026, 11, 5): 12.0})

    def test_trim(self):
        days = {date(2026, 1, 1): 0, date(2026, 1, 2): 0, date(2026, 1, 3): 5, date(2026, 1, 4): 0,
                date(2026, 1, 5): 7, date(2026, 1, 6): 9}
        self.assertEqual(CI.trim(days, date(2026, 1, 6)),
                         {date(2026, 1, 3): 5, date(2026, 1, 4): 0, date(2026, 1, 5): 7})


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = HistoryStore(self.dir)
        self.addCleanup(self.store.close)
        self.stats = F.FuelStats(self.store, TZ)
        FakeApi.devices = {"123": {"name": "boiler"}}
        FakeApi.years = [2026, 2027]
        FakeApi.days = {(2026, 12): {30: 40.0, 31: 35.5}, (2027, 1): {1: 30.0, 14: 20.0, 15: 99.0}}
        FakeApi.calls = []

    def make(self, env=ENV):
        return CI.CloudFuelImport(self.store, self.stats, environ=env, api_factory=FakeApi,
                                  today=lambda: TODAY, pause=0)

    def run_import(self, imp):
        imp.start()
        self.assertTrue(wait_for(lambda: imp.status()["state"] != "running"))
        return imp.status()

    def test_imports_once_without_duplicates(self):
        imp = self.make()
        st = self.run_import(imp)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["total"], 13)   # every month of 2026 and January 2027
        self.assertEqual(FakeApi.calls[-1], ("123", 2027, 1))
        cloud = self.store.cloud_fuel()
        self.assertEqual(min(cloud), "2026-12-30")       # empty days before the first use dropped
        self.assertNotIn("2027-01-15", cloud)            # today is not final
        self.assertEqual(st["stored"]["kg"], 40 + 35.5 + 30 + 20)
        st = self.run_import(imp)                        # again: replaced, not added
        self.assertEqual(st["stored"]["kg"], 125.5)
        self.assertEqual(st["stored"]["days"], len(cloud))

    def test_clear(self):
        imp = self.make()
        self.run_import(imp)
        st = imp.clear()
        self.assertEqual(st["stored"]["days"], 0)
        self.assertEqual(self.store.cloud_fuel(), {})

    def test_not_configured_and_several_devices(self):
        imp = self.make(env={})
        self.assertFalse(imp.configured)
        with self.assertRaises(ValueError):
            imp.start()
        FakeApi.devices = {"1": {}, "2": {}}
        st = self.run_import(self.make())
        self.assertEqual(st["state"], "error")
        self.assertIn("ESTYMA_DEVICE_ID", st["message"])
        st = self.run_import(self.make(env={**ENV, "ESTYMA_DEVICE_ID": "2"}))
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(FakeApi.calls[-1][0], "2")


class MergeTest(unittest.TestCase):
    """Imported cloud days in the fuel statistics."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = HistoryStore(self.dir)
        self.addCleanup(self.store.close)
        self.stats = F.FuelStats(self.store, TZ)

    def counter(self, t, kg):
        self.store.record(t, report(r8484_1=round(kg * 10)))

    def test_cloud_only_before_first_local_reading(self):
        self.store.put_cloud_fuel({"2027-01-12": 30.0, "2027-01-13": 32.0, "2027-01-14": 10.0,
                                   "2027-01-15": 99.0})
        # local recording starts at noon on the 14th: 15 kg there, so the cloud's
        # 10 kg loses; from the 15th on only local readings count
        self.counter(ts(2027, 1, 14, 12, 0), 1000)
        for h in range(22):   # hourly keyframes, no recording gap
            self.counter(ts(2027, 1, 14, 12, 0) + 3600 * (h + 1), 1015 if h < 12 else 1040)
        now = ts(2027, 1, 15, 12)
        days = self.stats.buckets("day", 5, now)
        self.assertEqual([(d["start"], d["kg"], d["cloud_kg"]) for d in days], [
            ("2027-01-11", None, None), ("2027-01-12", 30.0, 30.0), ("2027-01-13", 32.0, 32.0),
            ("2027-01-14", 15.0, 0.0), ("2027-01-15", 25.0, 0.0)])
        week = self.stats.buckets("week", 1, now)[0]     # Monday the 11th
        self.assertEqual((week["kg"], week["cloud_kg"]), (102.0, 62.0))
        snap = self.stats.snapshot("day", 3, now)
        self.assertEqual(snap["cloud"], {"first": "2027-01-12", "last": "2027-01-14", "days": 3})
        self.assertEqual(snap["first"], ts(2027, 1, 12))
        self.assertEqual(snap["local_first"], ts(2027, 1, 14, 12))
        self.assertEqual(snap["summary"]["today"], 25.0)

    def test_first_day_takes_the_larger_number(self):
        self.store.put_cloud_fuel({"2027-01-14": 36.0})
        self.counter(ts(2027, 1, 14, 18, 0), 1000)
        self.counter(ts(2027, 1, 14, 22, 0), 1008)
        day = self.stats.buckets("day", 2, ts(2027, 1, 15, 1))[0]
        self.assertEqual((day["kg"], day["cloud_kg"]), (36.0, 36.0))

    def test_cloud_without_local_readings_and_clear(self):
        self.store.put_cloud_fuel({"2027-01-12": 30.0})
        now = ts(2027, 1, 15, 12)
        self.assertEqual(self.stats.summary(now)["month"], 30.0)
        self.store.clear_cloud_fuel()
        self.assertIsNone(self.stats.summary(now)["month"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = HistoryStore(self.dir)
        self.addCleanup(self.store.close)
        FakeApi.devices = {"123": {"name": "boiler"}}
        FakeApi.days = {(2027, 1): {3: 12.0}}
        FakeApi.years = [2027]
        self.imp = CI.CloudFuelImport(self.store, None, environ=ENV, api_factory=FakeApi,
                                      today=lambda: TODAY, pause=0)

    def call(self, srv, method="GET", body=None, auth=True):
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/fuel-import", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(b"u:p").decode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_start_needs_ui_auth(self):
        srv = start_web("127.0.0.1", 0, self.dir, fuel_import=self.imp)
        self.addCleanup(srv.shutdown)
        code, st = self.call(srv, auth=False)
        self.assertEqual((code, st["ui_auth"], st["configured"]), (200, False, True))
        code, res = self.call(srv, "POST", {"action": "start"}, auth=False)
        self.assertEqual(code, 403)

    def test_start_and_clear(self):
        srv = start_web("127.0.0.1", 0, self.dir, "u", "p", fuel_import=self.imp)
        self.addCleanup(srv.shutdown)
        self.assertEqual(self.call(srv, "POST", {"action": "nope"})[0], 400)
        code, res = self.call(srv, "POST", {"action": "start"})
        self.assertEqual(code, 200, res)
        self.assertTrue(wait_for(lambda: self.call(srv)[1]["state"] == "done"))
        self.assertEqual(self.call(srv)[1]["stored"]["days"], 12)   # Jan 3 to 14
        code, res = self.call(srv, "POST", {"action": "clear"})
        self.assertEqual((code, res["stored"]["days"]), (200, 0))


if __name__ == "__main__":
    unittest.main()

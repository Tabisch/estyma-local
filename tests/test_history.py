"""Compact register history (history.py) and the raw log switch (raw_logs.py)."""

import base64
import gzip
import json
import os
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone

from fixtures import report, request_body

from estyma_local import history as H
from estyma_local import raw_logs as R
from estyma_local.monitor import Monitor
from estyma_local.server import ExchangeLog
from estyma_local.web import start_web

T0 = 1_800_000_000  # 2027-01-15


def record(ts: float, regs: dict, **extra) -> dict:
    body = request_body(regs).decode("latin-1")
    return {"time": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds"),
            "request": {"path": "/device/connect", "body": {"length": len(body), "text": body}},
            "response": {"status": 200, "body": {"text": "ODCZYT&1;8193,1;KONIEC&"}}, **extra}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = H.HistoryStore(self.dir, clock=lambda: T0 + 10**6)

    def tearDown(self):
        self.store.close()

    def test_only_changes_between_keyframes(self):
        regs = report()
        self.assertEqual(self.store.record(T0, regs), len(regs))       # first: all
        self.assertEqual(self.store.record(T0 + 20, regs), 0)          # nothing changed
        self.assertEqual(self.store.record(T0 + 40, report(r8193_1=706)), 1)
        self.assertEqual(self.store.record(T0 + H.KEYFRAME_S, report(r8193_1=706)), len(regs))
        self.assertEqual(self.store.series(8193), [(T0, 705), (T0 + 40, 706), (T0 + H.KEYFRAME_S, 706)])

    def test_replay_gives_full_reports(self):
        self.store.record(T0, report())
        self.store.record(T0 + 20, report())
        self.store.record(T0 + 40, report(r8193_1=706))
        out = list(self.store.replay(T0 + 30))
        self.assertEqual(len(out), 1)
        t, regs = out[0]
        self.assertEqual(t, T0 + 40)
        self.assertEqual(regs, report(r8193_1=706))  # unchanged values come from before `since`

    def test_replay_drops_keys_no_longer_reported(self):
        self.store.record(T0, report())
        gone = report()
        del gone[(8466, 1)]
        t = T0
        for _ in range(3):
            t += H.KEYFRAME_S
            self.store.record(t, gone)
        last = list(self.store.replay(T0))[-1][1]
        self.assertNotIn((8466, 1), last)
        self.assertIn((8193, 1), last)

    def test_ingest_skips_mirror_and_other_paths(self):
        self.store.ingest(record(T0, report(), mirror=True))
        self.store.ingest({"time": "x", "request": {"path": "/other"}})
        self.assertEqual(self.store.status()["values"], 0)
        self.store.ingest(record(T0, report()))
        self.assertEqual(self.store.status()["values"], len(report()))


class RawFilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_pack_appends_members(self):
        plain = os.path.join(self.dir, "exchanges-2027-01-15.jsonl")
        rec = record(T0, report())
        for i in range(2):
            with open(plain, "w") as fh:
                fh.write(json.dumps(dict(rec, id=i)) + "\n")
            R.pack(plain, truncate=(i == 0))
        self.assertFalse(os.path.exists(plain))
        got = list(R.read_records(plain + ".gz"))
        self.assertEqual([r["id"] for r in got], [0, 1])
        self.assertEqual(got[0]["request"]["body"]["text"], rec["request"]["body"]["text"])
        with gzip.open(plain + ".gz", "rt") as fh:  # plain gzip tools read it too
            self.assertEqual(len(fh.readlines()), 2)

    def test_cut_short_gz_reads_what_is_there(self):
        path = os.path.join(self.dir, "x.jsonl.gz")
        data = gzip.compress(b"".join(b'{"n": %d}\n' % i for i in range(2000)))
        with open(path, "wb") as fh:
            fh.write(data[: len(data) - 40])
        got = list(R.read_records(path))
        self.assertGreater(len(got), 100)
        self.assertEqual(got[0], {"n": 0})

    def test_setting_persists(self):
        s = R.RawLogSetting(self.dir, environ={})
        self.assertFalse(s.enabled)
        self.assertTrue(R.RawLogSetting(self.dir, environ={"RAW_LOGS": "1"}).enabled)
        seen = []
        s.listeners.append(seen.append)
        s.configure(True)
        self.assertEqual(seen, [True])
        self.assertTrue(R.RawLogSetting(self.dir, environ={"RAW_LOGS": "0"}).enabled)
        with self.assertRaises(ValueError):
            s.configure("yes")


class ExchangeLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.raw = R.RawLogSetting(self.dir, environ={})
        self.log = ExchangeLog(self.dir, 0, raw=self.raw)
        self.got = []
        self.log.listeners.append(self.got.append)

    def names(self):
        return sorted(n for n in os.listdir(self.dir) if n.startswith("exchanges-"))

    def test_off_writes_nothing_but_feeds_listeners(self):
        self.log.write(record(time.time(), report()))
        self.assertEqual(self.names(), [])
        self.assertEqual(len(self.got), 1)

    def test_on_packs_at_hour_change_and_when_switched_off(self):
        self.raw.configure(True)
        self.log.write(record(time.time(), report()))
        today = f"exchanges-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
        self.assertEqual(self.names(), [today])
        self.log._hour = self.log._hour[:11] + "xx"  # pretend the hour changed
        self.log.write(record(time.time(), report()))
        self.assertEqual(self.names(), [today, today + ".gz"])
        self.raw.configure(False)
        recs = [r for p in R.exchange_files(self.dir) for r in R.read_records(p)]
        self.assertEqual(len(recs), 2)
        self.assertEqual(os.path.getsize(os.path.join(self.dir, today)), 0)
        self.raw.configure(True)
        self.log.pack_current()  # nothing written since: no-op

    def test_form_bodies_logged_once(self):
        rec = self.log.body_record(b"a=1&b=2", "application/x-www-form-urlencoded")
        self.assertEqual(rec, {"length": 7, "text": "a=1&b=2"})
        self.assertEqual(self.log.body_record(b'{"x": 1}', "application/json")["json"], {"x": 1})


class MonitorRefillTest(unittest.TestCase):
    def test_charts_refilled_from_store(self):
        d = tempfile.mkdtemp()
        now = time.time()
        old = H.HistoryStore(d)
        for i in range(10):
            old.record(now - 3600 + 60 * i, report(r8193_1=700 + i))
        old.close()
        store = H.HistoryStore(d)
        mon = Monitor(d, 48, store=store)
        mon.load_history()
        snap = mon.snapshot()
        self.assertEqual(len(snap["history"]["t"]), 10)
        self.assertEqual(snap["history"]["boiler"][-1], mon.snapshot()["current"]["boiler"]["value"])
        store.close()


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.raw = R.RawLogSetting(self.dir, environ={})

    def call(self, srv, method="GET", body=None, auth=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/raw-logs",
                                     method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_switch_needs_auth(self):
        srv = start_web("127.0.0.1", 0, self.dir, no_login_for_tests=True, raw_logs=self.raw)
        self.addCleanup(srv.shutdown)
        st, j = self.call(srv)
        self.assertEqual((st, j["enabled"], j["ui_auth"]), (200, False, False))
        st, _ = self.call(srv, "POST", {"enabled": True})
        self.assertEqual(st, 403)
        self.assertFalse(self.raw.enabled)

    def test_switch_with_auth(self):
        srv = start_web("127.0.0.1", 0, self.dir, "u", "p", raw_logs=self.raw)
        self.addCleanup(srv.shutdown)
        st, j = self.call(srv, "POST", {"enabled": True}, auth="u:p")
        self.assertEqual((st, j["enabled"]), (200, True))
        self.assertTrue(self.raw.enabled)


if __name__ == "__main__":
    unittest.main()

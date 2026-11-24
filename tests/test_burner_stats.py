"""Burner statistics: starts, failed starts, error stops, burner hours."""

import base64
import json
import shutil
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone

from fixtures import report

from estyma_local import burner_stats as B
from estyma_local.controller import Controller
from estyma_local.history import HistoryStore, STALE_S
from estyma_local import mqtt_publisher as MP
from estyma_local.web import start_web

UTC = timezone.utc
DAY = datetime(2026, 11, 14, tzinfo=UTC).timestamp()


def at(h, m=0, day=0):
    return int(DAY + day * 86400 + h * 3600 + m * 60)


class FakeStore:
    def __init__(self, rows):
        self.rows = sorted(rows)

    def series(self, reg, idx=1, since=0, until=None):
        return [r for r in self.rows if r[0] >= since]


def raw(state):
    return 256 + state               # 8-bit register, reported as 0x100 + value


# The shape of the Nov 14 capture: a failed ignition (-> burn-out), one that
# ends in cleaning, then good starts with cleaning pauses and an error stop.
SEQ = [(at(9, 28), 0), (at(10, 7), 7), (at(10, 8), 1), (at(10, 19), 6), (at(10, 20), 7),
       (at(10, 21), 1), (at(10, 30), 7), (at(10, 32), 1), (at(10, 46), 2), (at(10, 53), 5),
       (at(11, 27), 10), (at(11, 28), 7), (at(11, 29), 1), (at(11, 31), 5), (at(12, 31), 6),
       (at(12, 41), 0)]


def with_keyframes(seq, now):
    """Rows like the history store writes them: each change, and the current
    value again at least once an hour (``gaps``: no rows in between)."""
    rows = []
    for (t, s), nxt in zip(seq, [x[0] for x in seq[1:]] + [now]):
        rows.append((t, raw(s)))
        k = t + 3600
        while k < nxt:
            rows.append((k, raw(s)))
            k += 3600
    return rows


def stats(seq=SEQ, now=at(23), keyframes=True):
    rows = with_keyframes(seq, now) if keyframes else [(t, raw(s)) for t, s in seq]
    return B.BurnerStats(FakeStore(rows), UTC, clock=lambda: now)


class CountTest(unittest.TestCase):
    def test_day(self):
        d = stats().period(at(0), at(0, day=1))
        self.assertEqual((d["starts"], d["failed"], d["errors"]), (4, 2, 1))
        # flame: 10:46-11:27 (41 min) + 11:31-12:31 (60 min)
        self.assertEqual(d["hours"], round(101 / 60, 2))
        self.assertEqual(d["avg_burn_min"], 50.5)
        self.assertFalse(d["estimated"])

    def test_glitch_ignored(self):
        seq = SEQ[:9] + [(at(10, 47), None)] + SEQ[9:]
        rows = [(t, raw(s) if s is not None else 0x7FFF1234) for t, s in seq if s is not None]
        rows += [(at(10, 47), 0x7FFF1234)] + [(at(h), raw(0)) for h in range(13, 24)]
        b = B.BurnerStats(FakeStore(rows), UTC, clock=lambda: at(23))
        self.assertEqual(b.period(at(0), at(0, day=1))["starts"], 4)

    def test_still_burning_counts_until_now(self):
        b = stats(SEQ[:10], now=at(11, 23))       # modulating since 10:53, keyframes hourly
        self.assertAlmostEqual(b.period(at(0), at(0, day=1))["hours"], 37 / 60, places=2)
        self.assertEqual(b.period(at(0), at(0, day=1))["failed"], 2)

    def test_burn_split_over_midnight(self):
        seq = [(at(22), 0), (at(23), 1), (at(23, 10), 5), (at(1, day=1), 6), (at(1, 10, day=1), 0)]
        b = stats(seq, now=at(12, day=1))
        self.assertAlmostEqual(b.period(at(0), at(0, day=1))["hours"], 50 / 60, places=2)
        self.assertEqual(b.period(at(0, day=1), at(0, day=2))["hours"], 1.0)
        self.assertEqual(b.period(at(0, day=1), at(0, day=2))["starts"], 0)

    def test_gap_is_not_counted(self):
        seq = [(at(8), 1), (at(8, 5), 5), (at(9, 5), 5), (at(9, 5) + STALE_S + 60, 5),
               (at(15), 6)]
        b = stats(seq, now=at(16), keyframes=False)
        d = b.period(at(0), at(0, day=1))
        self.assertTrue(d["estimated"])
        self.assertLess(d["hours"], 15 - 8)
        self.assertEqual(d["starts"], 0)          # the first ignition was already running

    def test_outcome_in_gap_is_unknown(self):
        seq = [(at(7), 0), (at(8), 1), (at(8) + STALE_S + 60, 5)]
        d = stats(seq, now=at(8) + STALE_S + 120, keyframes=False).period(at(0), at(0, day=1))
        self.assertEqual((d["starts"], d["failed"]), (1, 0))

    def test_buckets_and_summary(self):
        b = stats(now=at(12, day=1))
        days = b.buckets("day", 3)
        self.assertEqual([x["start"] for x in days], ["2026-11-13", "2026-11-14", "2026-11-15"])
        self.assertIsNone(days[0]["starts"])          # before the first reading
        self.assertEqual(days[1]["starts"], 4)
        self.assertEqual(days[2]["starts"], 0)
        s = b.summary()
        self.assertEqual(s["yesterday"]["starts"], 4)
        self.assertEqual(s["week"]["starts"], 4)      # Mon 2026-11-09 .. Sun
        snap = b.snapshot("week", 2)
        self.assertEqual(snap["buckets"][-1]["starts"], 4)
        with self.assertRaises(ValueError):
            b.buckets("hour")

    def test_from_history_store(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        store = HistoryStore(d)
        self.addCleanup(store.close)
        for t, s in SEQ:
            store.record(t, report(r8199_1=raw(s)))
        b = B.BurnerStats(store, UTC, clock=lambda: at(23))
        self.assertEqual(b.period(at(0), at(0, day=1))["starts"], 4)
        store.record(at(13), report(r8199_1=raw(1)))     # picked up on the next query
        self.assertEqual(b.period(at(0), at(0, day=1))["starts"], 5)


class OutputTest(unittest.TestCase):
    def test_api(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ui = start_web("127.0.0.1", 0, d, "u", "p", burner=stats(now=at(23)))
        self.addCleanup(ui.shutdown)
        h = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
        url = f"http://127.0.0.1:{ui.server_address[1]}/api/burner?period=day&count=2"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h)) as r:
            out = json.load(r)
        self.assertEqual(out["buckets"][-1]["starts"], 4)
        self.assertEqual(out["state"], 0)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(url.replace("day", "hour"), headers=h))
        self.assertEqual(cm.exception.code, 400)

    def test_mqtt_state(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ctl = Controller("proxy", d, state_file=f"{d}/s.json")
        ctl.observe_report(report())
        b = stats(now=at(10, day=1))
        st, _ = MP.StateBuilder(burner=b).build(ctl)
        self.assertEqual(st["burner_starts_today"], 0)
        self.assertEqual(st["burner_starts_yesterday"], 4)
        self.assertEqual(st["burner_avg_burn_yesterday"], 50.5)
        keys = {k for _, k, _, _ in MP.ENTITIES}
        self.assertTrue({"burner_starts_today", "burner_hours_today",
                         "burner_failed_starts_today"} <= keys)


if __name__ == "__main__":
    unittest.main()

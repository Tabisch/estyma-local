"""Raw register table: discovery, glitch filtering, persistence, web API."""

import http.client
import json
import tempfile
import unittest

from fixtures import cloud_reply, report, request_body  # noqa: F401  (sets sys.path)

from monitor import Monitor
from registers import CATALOG, RegisterTable, decode
from log_ui import start_log_ui


def record(ts_iso, regs, reply=""):
    return {"time": ts_iso, "request": {"path": "/device/connect",
                                        "body": {"text": request_body(regs).decode()}},
            "response": {"status": 200, "body": {"text": reply}}}


class DecodeTest(unittest.TestCase):
    def test_decodes(self):
        self.assertEqual(decode(705, "d10"), 70.5)
        self.assertEqual(decode(0x15A, "u8"), 90)               # 0x100 + 90
        self.assertEqual(decode(0x01E80090, "u16/10"), 14.4)
        self.assertEqual(decode(0xFFF6, "s16"), -10)
        self.assertEqual(decode(4194240, "hex"), "0x3FFFC0")
        self.assertEqual(decode(0x3008C, "alarm"), "code 140, state 3")

    def test_catalog_covers_known_registers(self):
        for reg in (8193, 8195, 8199, 8484, 8790, 8818, 16411, 16431, 16451, 16460, 8510):
            self.assertIn(reg, CATALOG)

    def test_service_settings(self):
        # All 45 service-panel registers (docs/service-settings.md) have a name.
        service = [8712, 8849, 8711, 14088, 8556, 8819, 8816, 8817, *range(8801, 8806), 8841,
                   8790, 14084, 12300, 8848, 8811, 10650, 8818, 8850, 13829, 8526, 8530, 14092,
                   14091, 14152, 14080, 14101, 14102, 14103, 8791, 8901, 8902, 16396, 16397,
                   8905, 8906, 8909, 8910, 8514, 8515, 8518, 8519]
        self.assertEqual(len(set(service)), 45)
        for reg in service:
            self.assertEqual(CATALOG[reg][4], "confirmed", reg)
        self.assertIn("pumps", CATALOG[8711][0])
        self.assertEqual(decode(65526, CATALOG[8819][2]), -10)
        self.assertEqual(decode(0x11B, CATALOG[8850][2]), 2.7)


class TableTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_new_registers_appear_and_glitches_do_not_stick(self):
        t = RegisterTable(self.dir)
        t.observe(100, {(8193, 1): 705})
        t.observe(120, {(8193, 1): 705, (8790, 1): 90, (8816, 1): 0x11E})  # service panel read
        t.observe(140, {(8193, 1): 31981648})                        # one-report glitch
        rows = {(r["reg"], r["idx"]): r for r in t.snapshot()}
        self.assertEqual(rows[(8193, 1)]["value"], 70.5)
        self.assertEqual(rows[(8193, 1)]["raw"], 31981648)
        self.assertEqual(rows[(8790, 1)]["label"], "Initial fuel dose")
        self.assertIsNone(rows[(8790, 1)]["stable"])                 # seen once
        self.assertEqual(rows[(8790, 1)]["value"], 90)
        self.assertEqual(rows[(8790, 1)]["first"], 120)
        self.assertEqual(rows[(8816, 1)]["value"], 30)                # 0x100 + 30

    def test_value_names_and_ranges(self):
        t = RegisterTable(self.dir)
        t.observe(1, {(8194, 1): 2, (8514, 1): 0x100, (8818, 1): 144, (8195, 3): 60})
        rows = {(r["reg"], r["idx"]): r for r in t.snapshot()}
        self.assertEqual(rows[(8194, 1)]["value_name"], ["automatic", "Automatik"])
        self.assertEqual(rows[(8514, 1)]["value_name"][0], "manual")
        self.assertEqual(rows[(8818, 1)]["range"], (5.0, 30.0))
        self.assertIsNone(rows[(8195, 3)]["range"])

    def test_unknown_and_requested_only(self):
        t = RegisterTable(self.dir)
        t.observe(100, {(9999, 1): 7}, requested=[(9999, 1), (12345, 3)])
        rows = {(r["reg"], r["idx"]): r for r in t.snapshot()}
        self.assertEqual(rows[(9999, 1)]["conf"], "unknown")
        self.assertIsNone(rows[(12345, 3)]["last"])
        self.assertEqual(rows[(12345, 3)]["requested"], 100)

    def test_changes_and_persistence(self):
        t = RegisterTable(self.dir)
        for ts, v in ((1, 60), (2, 60), (3, 61), (4, 61)):
            t.observe(ts, {(8195, 1): v})
        t.save(force=True)
        again = RegisterTable(self.dir)
        row = again.snapshot()[0]
        self.assertEqual((row["value"], row["changes"], row["count"]), (61, 1, 4))
        again.observe(3, {(8195, 1): 61})                            # replayed log line
        self.assertEqual(again.snapshot()[0]["count"], 4)


class MonitorFeedTest(unittest.TestCase):
    def test_monitor_feeds_table_and_api(self):
        d = tempfile.mkdtemp()
        mon = Monitor(d)
        mon.ingest(record("2026-11-08T10:00:00+00:00", report(r8790_1=90), cloud_reply()))
        regs = {(r["reg"], r["idx"]): r for r in mon.registers.snapshot()}
        self.assertIn((8790, 1), regs)
        self.assertIn((8471, 1), regs)            # asked for by the cloud, never reported
        ui = start_log_ui("127.0.0.1", 0, d, monitor=mon)
        try:
            c = http.client.HTTPConnection("127.0.0.1", ui.server_address[1], timeout=5)
            c.request("GET", "/api/registers")
            body = json.loads(c.getresponse().read())
            self.assertTrue(any(r["reg"] == 8790 for r in body["registers"]))
            c.request("GET", "/registers")
            res = c.getresponse()
            self.assertEqual(res.status, 200)
            self.assertIn(b"/api/registers", res.read())
        finally:
            ui.shutdown()


if __name__ == "__main__":
    unittest.main()

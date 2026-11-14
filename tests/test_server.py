"""End to end: fake cloud <- proxy (hybrid / local) <- fake controller."""

import base64
import http.client
import json
import tempfile
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fixtures import P, cloud_reply, report, request_body

from estyma_local import server as E
from estyma_local.cloud_fallback import CloudFallback
from estyma_local.controller import Controller
from estyma_local.web import start_web


class FakeCloud(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    writes = ()
    hits = 0
    delay = 0.0
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).hits += 1
        if self.delay:
            time.sleep(self.delay)
        if self.status != 200:
            msg = b"<html>Service Temporarily Unavailable</html>"
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        body = cloud_reply(self.writes).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


class ServerTest(unittest.TestCase):
    def setUp(self):
        FakeCloud.writes, FakeCloud.hits = (), 0
        FakeCloud.delay, FakeCloud.status = 0.0, 200
        self.cloud = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
        self.cloud_port = serve(self.cloud)
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        self.cloud.shutdown()
        for s in getattr(self, "servers", []):
            s.shutdown()

    def start(self, mode, **kw):
        ctl = Controller(mode, self.dir, writes_enabled=True, allow_power_writes=True,
                         clock_sync_interval=kw.pop("clock_sync", 0),
                         allow_untested_writes=kw.pop("untested", False))
        ctl.settings_due = kw.pop("read_settings", False)
        log = E.ExchangeLog(self.dir, 0)
        srv = E.make_server("127.0.0.1", 0, f"http://127.0.0.1:{self.cloud_port}", 5, log, ctl, **kw)
        ui = start_web("127.0.0.1", 0, self.dir, "u", "p", controller=ctl)
        self.servers = [srv, ui]
        self.port, self.ui_port = serve(srv), ui.server_address[1]
        return ctl

    def poll(self, regs):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/device/connect", body=request_body(regs),
                  headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        r = c.getresponse()
        return r.status, r.read().decode()

    def ui(self, method, path, body=None, auth=True, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.ui_port, timeout=5)
        h = {"Content-Type": ctype}
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(b"u:p").decode()
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"null")
        except ValueError:
            return r.status, raw.decode()

    def test_hybrid_injects_until_readback(self):
        self.start("hybrid")
        status, text = self.poll(report())
        self.assertEqual(text, cloud_reply())  # nothing pending: untouched
        code, res = self.ui("POST", "/api/set", {"key": "boiler_target", "value": 62})
        self.assertEqual(code, 200, res)
        _, text = self.poll(report())
        self.assertEqual(text, cloud_reply([(8195, 1, 62)]))
        _, text = self.poll(report())  # not applied yet: sent again
        self.assertIn("ZAPIS&;8195,1=62;4112,1=1;&KONIEC<br>", text)
        _, text = self.poll(report(r8195_1=62))
        self.assertEqual(text, cloud_reply())
        _, snap = self.ui("GET", "/api/control")
        self.assertEqual(snap["pending"], [])
        self.assertEqual(snap["recent"][0]["event"], "confirmed")

    def test_hybrid_leaves_cloud_writes_alone(self):
        self.start("hybrid")
        self.poll(report())
        self.ui("POST", "/api/set", {"key": "boiler_target", "value": 62})
        FakeCloud.writes = [(8549, 1, 1794099835)]
        _, text = self.poll(report())
        self.assertEqual(text, cloud_reply([(8549, 1, 1794099835)]))

    def test_ui_guards(self):
        self.start("hybrid")
        self.poll(report())
        code, _ = self.ui("POST", "/api/set", {"key": "boiler_target", "value": 62}, auth=False)
        self.assertEqual(code, 401)
        code, _ = self.ui("POST", "/api/set", {"key": "boiler_target", "value": 62},
                          ctype="text/plain")
        self.assertEqual(code, 415)
        code, res = self.ui("POST", "/api/set", {"key": "boiler_target", "value": 99})
        self.assertEqual(code, 409)
        self.assertIn("between", res["message"])

    def test_hybrid_reads_settings(self):
        self.start("hybrid", read_settings=True)
        _, text = self.poll(report())
        rl = P.parse_read_list(text)
        base = P.parse_read_list(cloud_reply())
        self.assertEqual(rl[:len(base)], base)
        self.assertIn((8712, 1), rl[len(base):])
        self.assertIn(f"ODCZYT&{len(rl)};", text)
        _, snap = self.ui("GET", "/api/settings")
        self.assertIsNotNone(snap["settings"]["sent"])
        self.poll(report(**{f"r{r}_{i}": 0x100 for r, i in rl[len(base):]}))
        _, text = self.poll(report())
        self.assertEqual(text, cloud_reply())  # read done: reply untouched again
        code, res = self.ui("POST", "/api/read-settings", {})
        self.assertEqual(code, 200, res)
        _, text = self.poll(report())
        self.assertIn("8712,1;", text)

    def test_settings_page_and_untested_write(self):
        self.start("hybrid", untested=True)
        self.poll(report(r8712_1=10))
        code, page = self.ui("GET", "/heating")
        self.assertEqual(code, 200)
        self.assertIn("Estyma heating settings", page)
        code, page = self.ui("GET", "/settings")  # app settings, not a redirect
        self.assertEqual(code, 200)
        self.assertIn("Estyma app settings", page)
        self.assertIn('id="cloud-toggle"', page)
        self.assertNotIn("Estyma heating settings", page)
        _, monitor = self.ui("GET", "/")
        self.assertNotIn('id="cloud-toggle"', monitor)  # switched on /settings only
        self.assertIn('href="/settings"', monitor)
        code, res = self.ui("POST", "/api/set", {"key": "hyst_on", "value": 8})
        self.assertEqual(code, 200, res)
        _, text = self.poll(report(r8712_1=10))
        self.assertIn("ZAPIS&;8712,1=8;4112,1=1;&KONIEC<br>", text)
        self.assertIn("8712,1;", text)  # read back even though not in the cloud's list
        self.poll(report(r8712_1=8))
        _, snap = self.ui("GET", "/api/settings")
        self.assertEqual(snap["pending"], [])
        items = {i["key"]: i for g in snap["groups"] for i in g["items"]}
        self.assertEqual(items["hyst_on"]["value"], 8)

    def test_pellet_batch_snapshots(self):
        self.start("hybrid")
        self.poll(report(r8712_1=10))
        code, page = self.ui("GET", "/batches")
        self.assertEqual(code, 200)
        self.assertIn("Estyma pellet batches", page)
        _, heating = self.ui("GET", "/heating")
        self.assertIn('href="/batches"', heating)
        code, res = self.ui("POST", "/api/snapshots/save", {"supplier": "", "date": ""})
        self.assertEqual(code, 409)
        code, res = self.ui("POST", "/api/snapshots/save",
                            {"supplier": "Supplier A", "date": "2026-11-01", "comment": "test"})
        self.assertEqual(code, 200, res)
        sid = res["snapshot"]["id"]
        _, lst = self.ui("GET", "/api/snapshots")
        self.assertEqual(lst["suppliers"], ["Supplier A"])
        items = {i["key"]: i for i in lst["snapshots"][0]["settings"]}
        self.assertEqual(items["hyst_on"]["value"], 10)
        code, res = self.ui("POST", "/api/snapshots/update", {"id": sid, "comment": "changed"})
        self.assertEqual((code, res["snapshot"]["comment"]), (200, "changed"))
        code, _ = self.ui("POST", "/api/snapshots/delete", {"id": sid}, auth=False)
        self.assertEqual(code, 401)
        code, _ = self.ui("POST", "/api/snapshots/delete", {"id": sid})
        self.assertEqual(code, 200)
        code, _ = self.ui("POST", "/api/snapshots/delete", {"id": sid})
        self.assertEqual(code, 404)

    def test_local_mode_answers_without_cloud(self):
        self.start("local", clock_sync=3600)
        status, text = self.poll(report())
        self.assertEqual(status, 200)
        self.assertEqual(FakeCloud.hits, 0)
        self.assertIn("ZAPIS&;8549,1=", text)  # clock sync on the first poll
        rl = P.parse_read_list(text)
        self.assertEqual(rl[-6:], P.alarm_slot_regs(11) + P.alarm_slot_regs(10))
        self.ui("POST", "/api/set", {"key": "boiler_on", "value": 0})
        _, text = self.poll(report())
        self.assertIn("ZAPIS&;8197,1=0;4112,1=1;&KONIEC<br>ODCZYT&", text)
        _, text = self.poll(report(r8197_1=256))
        self.assertNotIn("ZAPIS", text)

    def test_local_fallback(self):
        self.start("hybrid", local_fallback=True)
        self.cloud.shutdown()
        self.cloud.server_close()
        status, text = self.poll(report())
        self.assertEqual(status, 200)
        self.assertIn("ODCZYT&", text)


    def fallback(self, seconds):
        return CloudFallback(self.dir, environ={"CLOUD_FALLBACK_SECONDS": str(seconds)})

    def log_records(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("exchanges-"):
                with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                    out += [json.loads(line) for line in fh]
        return out

    def test_slow_cloud_answered_locally(self):
        fb = self.fallback(1)
        self.start("hybrid", fallback=fb)
        FakeCloud.delay = 2.5
        t0 = time.time()
        status, text = self.poll(report())
        took = time.time() - t0
        self.assertEqual(status, 200)
        self.assertLess(took, 2.0)
        self.assertGreaterEqual(took, 0.9)
        self.assertIn("ODCZYT&", text)
        self.assertIn("SERVER_INFO=REGISTER,1;DELAY,10;", text)
        self.assertEqual(P.parse_read_list(text)[:5], P.DEFAULT_BASE_READ_LIST[:5])
        self.assertEqual(fb.status()["counts"]["slow"], 1)
        # The cloud still got the report; its late reply is logged on its own.
        deadline = time.time() + 5
        while time.time() < deadline and not any(r.get("late_for") for r in self.log_records()):
            time.sleep(0.1)
        recs = self.log_records()
        main = [r for r in recs if r.get("request", {}).get("body")]
        late = [r for r in recs if r.get("late_for")]
        self.assertEqual(len(main), 1)
        self.assertTrue(main[0]["local"] and main[0]["fallback"])
        self.assertEqual(main[0]["fallback_reason"], "slow")
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["late_for"], main[0]["id"])
        self.assertEqual(late[0]["response"]["status"], 200)
        self.assertEqual(fb.status()["late_replies"], 1)
        self.assertEqual(FakeCloud.hits, 1)

    def test_fast_cloud_passes_through(self):
        fb = self.fallback(3)
        self.start("hybrid", fallback=fb)
        status, text = self.poll(report())
        self.assertEqual(text, cloud_reply())
        self.assertEqual(fb.status()["total"], 0)

    def test_cloud_5xx_answered_locally(self):
        fb = self.fallback(5)
        self.start("proxy", fallback=fb)
        FakeCloud.status = 503
        status, text = self.poll(report())
        self.assertEqual(status, 200)
        self.assertIn("SERVER_INFO=REGISTER,1;DELAY,10;", text)
        rec = self.log_records()[-1]
        self.assertEqual((rec["fallback_reason"], rec["cloud_status"]), ("cloud_error", 503))
        self.assertEqual(fb.status()["counts"]["cloud_error"], 1)

    def test_unreachable_cloud_answered_locally(self):
        fb = self.fallback(5)
        self.start("hybrid", fallback=fb)
        self.cloud.shutdown()
        self.cloud.server_close()
        status, text = self.poll(report())
        self.assertEqual(status, 200)
        self.assertIn("ODCZYT&", text)
        self.assertEqual(fb.status()["counts"]["unreachable"], 1)

    def test_zero_turns_it_off(self):
        fb = self.fallback(0)
        self.start("hybrid", fallback=fb)
        FakeCloud.status = 503
        status, _ = self.poll(report())
        self.assertEqual(status, 503)
        FakeCloud.status, FakeCloud.delay = 200, 1.2
        status, text = self.poll(report())
        self.assertEqual(text, cloud_reply())
        self.assertEqual(fb.status()["total"], 0)


if __name__ == "__main__":
    unittest.main()

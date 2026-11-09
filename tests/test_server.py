"""End to end: fake cloud <- proxy (hybrid / local) <- fake controller."""

import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fixtures import P, cloud_reply, report, request_body

import estyma_proxy as E
from controller import Controller
from log_ui import start_log_ui


class FakeCloud(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    writes = ()
    hits = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).hits += 1
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
        ui = start_log_ui("127.0.0.1", 0, self.dir, "u", "p", controller=ctl)
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
        c = http.client.HTTPConnection("127.0.0.1", self.ui_port, timeout=5)
        c.request("GET", "/settings", headers={"Authorization": "Basic " + base64.b64encode(b"u:p").decode()})
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 404)  # kept free for app settings, no redirect
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


if __name__ == "__main__":
    unittest.main()

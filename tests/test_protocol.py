import unittest
from datetime import datetime, timezone, timedelta

from fixtures import P, cloud_reply, report, request_body


class ProtocolTest(unittest.TestCase):
    def test_parse_request(self):
        form = P.loose_form(request_body(report()).decode())
        self.assertEqual(form["SOFT_VER"], " 1.12")
        self.assertEqual(form["NUMER RAMKI"], "33")
        regs = P.parse_report(form["ZAPISZ_ODCZYT"])
        self.assertEqual(regs[(8195, 1)], 60)
        self.assertEqual(regs[(8197, 1)], 257)

    def test_read_list_until_koniec(self):
        rl = P.parse_read_list(cloud_reply())
        self.assertEqual(len(rl), 64)
        self.assertIn((16411, 1), rl)
        self.assertIsNone(P.parse_read_list("503 Service Unavailable"))

    def test_parse_writes(self):
        text = "x -- ZAPIS&;8549,1=1794099835;4112,1=1;&KONIEC<br>ODCZYT&1;8193,1;KONIEC&<br>"
        self.assertEqual(P.parse_writes(text), [(8549, 1, 1794099835), (4112, 1, 1)])
        self.assertEqual(P.parse_writes(cloud_reply()), [])

    def test_inject_matches_cloud_layout(self):
        reply = cloud_reply()
        new = P.inject_writes(reply, [(8195, 1, 61)])
        self.assertEqual(new, cloud_reply([(8195, 1, 61)]))
        self.assertIn("-- ZAPIS&;8195,1=61;4112,1=1;&KONIEC<br>ODCZYT&63;", new)

    def test_no_inject_into_reply_with_writes_or_error(self):
        self.assertIsNone(P.inject_writes(cloud_reply([(8549, 1, 5)]), [(8195, 1, 61)]))
        self.assertIsNone(P.inject_writes("<html>503</html>", [(8195, 1, 61)]))
        self.assertIsNone(P.inject_writes(cloud_reply(), []))

    def test_build_reply(self):
        tz = timezone(timedelta(hours=1))
        now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        text = P.build_reply([(8193, 1), (8195, 3)], [(8197, 1, 0)], now=now, tz=tz)
        self.assertEqual(text,
                         "Next Daystat Update: 2026-11-09 00:00:00 (GMT 1) -- "
                         "ZAPIS&;8197,1=0;4112,1=1;&KONIEC<br>"
                         "ODCZYT&2;8193,1;8195,3;KONIEC&<br>"
                         "SERVER_INFO=REGISTER,1;DELAY,10;<br><br>wygenerowano w: 0.0010 sec.")
        self.assertEqual(P.parse_read_list(text), [(8193, 1), (8195, 3)])

    def test_local_unix_time(self):
        tz = timezone(timedelta(hours=1))
        now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(P.local_unix_time(now, tz), int(now.timestamp()) + 3600)

    def test_alarm_slot_regs(self):
        self.assertEqual(P.alarm_slot_regs(11), [(16451, 1), (16431, 1), (16411, 1)])
        self.assertEqual(P.alarm_slot_regs(-1), [(16459, 1), (16439, 1), (16419, 1)])


if __name__ == "__main__":
    unittest.main()


class AddReadsTest(unittest.TestCase):
    def test_appends_new_entries(self):
        reply = cloud_reply()
        before = P.parse_read_list(reply)
        out = P.add_reads(reply, [(8712, 1), (8193, 1), (8712, 1)])
        after = P.parse_read_list(out)
        self.assertEqual(after, before + [(8712, 1)])
        self.assertIn(f"ODCZYT&{len(after)};", out)
        self.assertTrue(out.endswith(reply[reply.index("KONIEC&"):]))

    def test_nothing_to_add(self):
        self.assertIsNone(P.add_reads(cloud_reply(), [(8193, 1)]))
        self.assertIsNone(P.add_reads("error page", [(8712, 1)]))

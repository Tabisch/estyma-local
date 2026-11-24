"""Weekly timer: text format, one ZAPIS for all 7 days, read-back per day."""

import unittest

from fixtures import P, cloud_reply, report

from estyma_local import controller as C
from estyma_local.controller import TIMER_KEYS, TooFast, WriteError
from test_controller import make

DAY = 0x3FFFC0                       # comfort 06-22
REGS = [8486 + 4 * d for d in range(7)]


def timer_report(masks=None, **changes):
    masks = masks or [DAY] * 7
    return report(**{f"r{r}_1": m for r, m in zip(REGS, masks)}, **changes)


class TextTest(unittest.TestCase):
    def test_round_trip(self):
        for text, mask in [("06-22", DAY), ("", 0), ("00-24", 0xFFFFFF),
                           ("06-09,16-22", 0b1111110000000111000000), ("23-24", 1 << 23)]:
            self.assertEqual(C.parse_timer_text(text), mask, text)
            self.assertEqual(C.timer_text(mask), text)

    def test_lenient_input(self):
        self.assertEqual(C.parse_timer_text(" 6-22 "), DAY)
        self.assertEqual(C.parse_timer_text("06:00-22:00"), DAY)
        self.assertEqual(C.parse_timer_text("none"), 0)

    def test_bad_input(self):
        for bad in ("22-06", "6", "a-b", "0-25", "5-5"):
            with self.assertRaises(ValueError, msg=bad):
                C.parse_timer_text(bad)

    def test_glitch_is_not_a_mask(self):
        w = C.WRITABLE["co1_timer_mon"]
        self.assertIsNone(w.readback(0x1000000 | DAY))
        self.assertEqual(w.readback(DAY), DAY)


class TimerWriteTest(unittest.TestCase):
    def test_all_days_in_one_zapis_until_readback(self):
        c, clock, _ = make()
        c.observe_report(timer_report())
        new = [DAY] * 7
        new[0] = C.parse_timer_text("05-22")
        res = c.request_timer(new)
        self.assertEqual(res["status"], "pending")
        w = c.next_writes(local_reply=False)
        self.assertEqual([r for r, _, _ in w], sorted(REGS, reverse=True))  # Sunday first, like the cloud
        self.assertEqual(dict((r, v) for r, _, v in w)[8486], new[0])
        zapis = P.format_writes(w)
        self.assertTrue(zapis.startswith("ZAPIS&;8510,1="))
        self.assertTrue(zapis.endswith("4112,1=1;&KONIEC<br>"))
        clock.t += 20
        c.observe_report(timer_report())            # not applied yet: sent again
        self.assertEqual(len(c.next_writes(local_reply=False)), 7)
        clock.t += 20
        c.observe_report(timer_report(new))
        self.assertEqual(c.pending, {})
        confirmed = [r for r in c.recent if r["event"] == "confirmed"]
        self.assertEqual([r["key"] for r in confirmed], ["co1_timer_mon"])  # only the changed day

    def test_one_day_by_key_keeps_the_others(self):
        c, _, _ = make()
        masks = [DAY, DAY, DAY, DAY, DAY, 0, 0]
        c.observe_report(timer_report(masks))
        c.request_write("co1_timer_sat", "08-20")
        sent = {r: v for r, _, v in c.next_writes(local_reply=False)}
        self.assertEqual(sent[8506], C.parse_timer_text("08-20"))
        self.assertEqual(sent[8510], 0)
        self.assertEqual(sent[8486], DAY)

    def test_second_edit_merges_with_pending(self):
        c, clock, _ = make()
        c.observe_report(timer_report())
        c.request_timer({"mon": 0})
        clock.t += 11
        c.request_timer({"tue": 0})
        sent = {r: v for r, _, v in c.next_writes(local_reply=False)}
        self.assertEqual((sent[8486], sent[8490], sent[8494]), (0, 0, DAY))

    def test_rate_limit_is_too_fast(self):
        c, clock, _ = make()
        c.observe_report(timer_report())
        c.request_timer({"mon": 0})
        with self.assertRaises(TooFast) as cm:
            c.request_timer({"tue": 0})
        self.assertGreater(cm.exception.wait, 0)

    def test_unchanged(self):
        c, _, _ = make()
        c.observe_report(timer_report())
        self.assertEqual(c.request_timer([DAY] * 7)["status"], "unchanged")
        self.assertEqual(c.next_writes(local_reply=False), [])

    def test_rejections(self):
        c, _, _ = make()
        c.observe_report(report())                     # timers not reported yet
        with self.assertRaisesRegex(WriteError, "not been read"):
            c.request_timer([0] * 7)
        c.observe_report(timer_report())
        with self.assertRaisesRegex(WriteError, "24-bit"):
            c.request_timer([1 << 24] + [0] * 6)
        with self.assertRaisesRegex(WriteError, "7 masks"):
            c.request_timer([0] * 6)
        with self.assertRaisesRegex(WriteError, "start must be before end"):
            c.request_timer({"mon": "22-06"})
        p, _, _ = make("proxy")
        p.observe_report(timer_report())
        with self.assertRaisesRegex(WriteError, "MODE"):
            p.request_timer([0] * 7)
        off, _, _ = make(writes_enabled=False)
        off.observe_report(timer_report())
        with self.assertRaisesRegex(WriteError, "WRITES_ENABLED"):
            off.request_timer([0] * 7)

    def test_cloud_write_cancels_whole_timer(self):
        c, _, _ = make()
        c.observe_report(timer_report())
        c.request_timer({"mon": 0})
        c.observe_cloud_reply(cloud_reply([(8490, 1, 0)]))
        self.assertEqual(c.pending, {})

    def test_cancel_cancels_all_days(self):
        c, _, _ = make()
        c.observe_report(timer_report())
        c.request_timer({"mon": 0})
        self.assertTrue(c.cancel_write("co1_timer_sun"))
        self.assertEqual(c.pending, {})

    def test_snapshot(self):
        c, _, _ = make()
        c.observe_report(timer_report())
        c.request_timer({"wed": 0})
        t = c.settings_snapshot()["timers"]
        self.assertTrue(t["allowed"])
        self.assertEqual([d["day"] for d in t["days"]], list(C.DAY_KEYS))
        self.assertEqual(t["days"][2]["pending"], 0)
        self.assertEqual(t["days"][0]["value"], DAY)
        # not in the grouped settings (pellet batch saves stay as they are)
        keys = {it["key"] for g in c.settings_snapshot()["groups"] for it in g["items"]}
        self.assertFalse(keys & set(TIMER_KEYS))


if __name__ == "__main__":
    unittest.main()

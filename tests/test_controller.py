import os
import tempfile
import unittest

from fixtures import P, cloud_reply, report

from estyma_local.controller import Controller, WriteError


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(mode="hybrid", **kw):
    d = tempfile.mkdtemp()
    clock = Clock()
    opts = dict(writes_enabled=True, allow_power_writes=True)
    opts.update(kw)
    return Controller(mode, d, clock=clock, **opts), clock, d


class WriteSafetyTest(unittest.TestCase):
    def test_read_only_by_default(self):
        c, _, _ = make(writes_enabled=False)
        c.observe_report(report())
        with self.assertRaisesRegex(WriteError, "WRITES_ENABLED"):
            c.request_write("boiler_target", 61)

    def test_proxy_mode_rejects(self):
        c, _, _ = make("proxy")
        c.observe_report(report())
        with self.assertRaisesRegex(WriteError, "MODE"):
            c.request_write("boiler_target", 61)

    def test_power_needs_extra_flag(self):
        c, _, _ = make(allow_power_writes=False)
        c.observe_report(report())
        with self.assertRaisesRegex(WriteError, "ALLOW_POWER_WRITES"):
            c.request_write("boiler_on", 0)
        c.request_write("boiler_target", 61)  # setpoints still fine

    def test_not_allow_listed(self):
        c, _, _ = make()
        c.observe_report(report())
        with self.assertRaises(WriteError):
            c.request_write("fuel_level", 50)

    def test_range_uses_controller_limits(self):
        c, _, _ = make()
        c.observe_report(report(r8195_4=75))
        self.assertEqual(c.limits("boiler_target"), (60, 75))
        with self.assertRaisesRegex(WriteError, "between 60 and 75"):
            c.request_write("boiler_target", 76)
        with self.assertRaises(WriteError):
            c.request_write("boiler_target", 59)
        with self.assertRaises(WriteError):
            c.request_write("boiler_target", 61.5)

    def test_glitched_limit_ignored(self):
        c, _, _ = make()
        c.observe_report(report(r8195_4=-1515913136))  # 0xA5A50050 -> low 16 bits 80
        self.assertEqual(c.limits("boiler_target"), (60, 80))

    def test_needs_a_report_first(self):
        c, _, _ = make()
        with self.assertRaisesRegex(WriteError, "no report"):
            c.request_write("boiler_target", 61)

    def test_noop_and_rate_limit(self):
        c, clock, _ = make()
        c.observe_report(report())
        self.assertEqual(c.request_write("boiler_target", 60)["status"], "unchanged")
        c.request_write("boiler_target", 61)
        clock.t += 2
        with self.assertRaisesRegex(WriteError, "too fast"):
            c.request_write("boiler_target", 62)
        clock.t += 10
        c.request_write("boiler_target", 62)  # replaces the pending value
        self.assertEqual(len(c.pending), 1)
        self.assertEqual(c.pending["boiler_target"].value, 62)

    def test_alarm_blocks_setpoints_not_power(self):
        c, _, _ = make()
        c.observe_report(report(r16460_1=269, r16412_1=0x3008C, r16452_1=0, r8199_1=266))
        self.assertTrue(c.alarm_active())
        with self.assertRaisesRegex(WriteError, "alarm"):
            c.request_write("boiler_target", 61)
        c.request_write("boiler_on", 0)

    def test_alarm_cleared(self):
        c, _, _ = make()
        c.observe_report(report(r16460_1=269, r16412_1=0x8C, r16452_1=1794000000))
        self.assertFalse(c.alarm_active())


class WriteFlowTest(unittest.TestCase):
    def test_repeat_until_readback(self):
        c, clock, d = make()
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        for _ in range(3):
            self.assertEqual(c.next_writes(local_reply=False), [(8195, 1, 61)])
            clock.t += 14
            c.observe_report(report())       # controller has not applied it yet
        self.assertEqual(c.pending["boiler_target"].sent, 3)
        c.observe_report(report(r8195_1=61))
        self.assertEqual(c.pending, {})
        self.assertEqual(c.next_writes(local_reply=False), [])
        events = [r["event"] for r in c.recent]
        self.assertEqual(events, ["requested", "sent", "confirmed"])
        files = [f for f in os.listdir(d) if f.startswith("writes-")]
        self.assertEqual(len(files), 1)

    def test_bool_readback(self):
        c, clock, _ = make()
        c.observe_report(report())
        c.request_write("boiler_on", 0)
        self.assertEqual(c.next_writes(local_reply=False), [(8197, 1, 0)])
        c.observe_report(report(r8197_1=256))
        self.assertEqual(c.pending, {})

    def test_timeout(self):
        c, clock, _ = make(write_timeout=60)
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        c.next_writes(local_reply=False)
        clock.t += 61
        c.observe_report(report())
        self.assertEqual(c.pending, {})
        self.assertEqual(c.recent[-1]["event"], "failed")

    def test_one_register_per_poll(self):
        c, _, _ = make()
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        c.request_write("boiler_on", 0)
        a = c.next_writes(local_reply=False)
        b = c.next_writes(local_reply=False)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertNotEqual(a, b)

    def test_cloud_write_supersedes(self):
        c, _, _ = make()
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        c.observe_cloud_reply(cloud_reply([(8195, 1, 65)]))
        self.assertEqual(c.pending, {})
        self.assertEqual(c.recent[-1]["event"], "superseded")

    def test_cancel(self):
        c, _, _ = make()
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        self.assertTrue(c.cancel_write("boiler_target"))
        self.assertEqual(c.next_writes(local_reply=False), [])


class LocalModeTest(unittest.TestCase):
    def test_clock_sync_first_then_writes(self):
        c, clock, _ = make("local")
        c.observe_report(report())
        c.request_write("boiler_target", 61)
        first = c.next_writes(local_reply=True)
        self.assertEqual(first[0][:2], P.CLOCK_REG)
        self.assertEqual(c.next_writes(local_reply=True), [(8195, 1, 61)])

    def test_clock_sync_off(self):
        c, _, _ = make("local", clock_sync_interval=0)
        self.assertEqual(c.next_writes(local_reply=True), [])

    def test_read_list_follows_alarm_counter(self):
        c, _, _ = make("local")
        c.observe_report(report())
        rl = c.read_list()
        self.assertEqual(rl[-6:], P.alarm_slot_regs(11) + P.alarm_slot_regs(10))
        c.observe_report(report(r16460_1=269))
        self.assertEqual(c.read_list()[-6:-3], P.alarm_slot_regs(12))

    def test_learns_read_list_and_persists(self):
        c, _, d = make("hybrid")
        reply = cloud_reply().replace("8510,1;", "8510,1;9999,1;")
        c.observe_cloud_reply(reply)
        self.assertIn((9999, 1), c.base_read_list)
        self.assertNotIn((16411, 1), c.base_read_list)
        c.observe_report(report())
        c.save_state(force=True)
        c2 = Controller("local", d)
        self.assertIn((9999, 1), c2.base_read_list)
        self.assertEqual(c2.raw(8195, 1), 60)


if __name__ == "__main__":
    unittest.main()


SETTINGS_REPORT = dict(r8526_1=0x100 + 120, r8526_3=0x100 + 50, r8526_4=0x100 + 150,
                       r8468_1=60, r8468_3=7, r8468_4=80, r8712_1=10, r8514_1=0x100)


class SettingsTest(unittest.TestCase):
    def test_untested_needs_extra_flag(self):
        c, _, _ = make()
        c.observe_report(report(**SETTINGS_REPORT))
        with self.assertRaisesRegex(WriteError, "ALLOW_UNTESTED_WRITES"):
            c.request_write("buffer_top_target", 50)
        c.request_write("boiler_target", 61)  # tested ones still fine

    def test_scaled_write_and_u8_readback(self):
        c, _, _ = make(allow_untested_writes=True)
        c.observe_report(report(**SETTINGS_REPORT))
        self.assertEqual(c.current("o2_30"), 120)
        with self.assertRaisesRegex(WriteError, "multiple of 0.1"):
            c.request_write("o2_30", 8.55)
        with self.assertRaisesRegex(WriteError, "between 5.0 and 15.0"):
            c.request_write("o2_30", 16)
        res = c.request_write("o2_30", 8.5)
        self.assertIn("8.5 %", res["message"])
        self.assertEqual(c.next_writes(local_reply=False), [(8526, 1, 85)])
        c.observe_report(report(**dict(SETTINGS_REPORT, r8526_1=0x100 + 85)))
        self.assertEqual(c.pending, {})

    def test_u8_limits_from_controller(self):
        c, _, _ = make(allow_untested_writes=True)
        c.observe_report(report(**dict(SETTINGS_REPORT, r8526_4=0x100 + 140)))
        self.assertEqual(c.limits("o2_30"), (50, 140))
        c.observe_report(report(**dict(SETTINGS_REPORT, r8526_3=0x100 + 150, r8526_4=0x100 + 60)))
        self.assertEqual(c.limits("o2_30"), (50, 150))  # min > max: ignored

    def test_needs_a_read_first(self):
        c, _, _ = make(allow_untested_writes=True)
        c.observe_report(report())
        with self.assertRaisesRegex(WriteError, "not been read"):
            c.request_write("hyst_on", 8)

    def test_enum_values(self):
        c, _, _ = make(allow_untested_writes=True)
        c.observe_report(report(**SETTINGS_REPORT))
        with self.assertRaisesRegex(WriteError, "not allowed"):
            c.request_write("co1_mode", 2)
        c.request_write("co1_mode", 1)
        self.assertEqual(c.next_writes(local_reply=False), [(8514, 1, 1)])

    def test_settings_reads(self):
        c, clock, _ = make(settings_read_interval=3600)
        reads = c.extra_reads()
        self.assertIn((8712, 1), reads)
        self.assertIn((8526, 4), reads)
        self.assertNotIn((8195, 1), reads)
        self.assertEqual(c.extra_reads(), [])  # sent, waiting for the report
        regs = report(**{f"r{r}_{i}": 0x100 for r, i in reads})
        clock.t += 15
        c.observe_report(regs)
        self.assertEqual(c.settings_read, clock.t)
        self.assertEqual(c.extra_reads(), [])
        clock.t += 3600
        self.assertIn((8712, 1), c.extra_reads())

    def test_settings_read_retried(self):
        c, clock, _ = make()
        self.assertTrue(c.extra_reads())
        c.observe_report(report())  # reply lost: settings not reported
        self.assertTrue(c.extra_reads())

    def test_pending_register_is_read(self):
        c, _, _ = make(allow_untested_writes=True)
        c.observe_report(report(**SETTINGS_REPORT))
        c.settings_due = False
        c.request_write("hyst_on", 8)
        self.assertEqual(c.extra_reads(), [(8712, 1), (8712, 3), (8712, 4)])
        self.assertEqual(c.extra_reads(present=[(8712, 1), (8712, 3), (8712, 4)]), [])

    def test_no_reads_in_proxy_mode(self):
        c, _, _ = make("proxy")
        self.assertEqual(c.extra_reads(), [])
        self.assertFalse(c.request_settings_read()["ok"])

    def test_settings_snapshot(self):
        c, _, _ = make()
        c.observe_report(report(**SETTINGS_REPORT))
        snap = c.settings_snapshot()
        self.assertEqual([g["key"] for g in snap["groups"]],
                         ["user", "boiler", "burner", "circuit1", "circuit2"])
        items = {i["key"]: i for g in snap["groups"] for i in g["items"]}
        self.assertEqual(items["o2_30"]["value"], 12.0)
        self.assertEqual((items["o2_30"]["min"], items["o2_30"]["max"]), (5.0, 15.0))
        self.assertEqual(items["co1_mode"]["options"]["0"], ["manual", "manuell"])
        self.assertFalse(items["o2_30"]["allowed"])
        self.assertEqual(items["o2_30"]["reason"], "ALLOW_UNTESTED_WRITES=0")
        self.assertTrue(items["boiler_target"]["allowed"])
        self.assertEqual(set(c.snapshot()["writable"]), {"boiler_target", "boiler_on"})

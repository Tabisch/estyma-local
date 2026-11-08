import os
import tempfile
import unittest

from fixtures import P, cloud_reply, report

from controller import Controller, WriteError


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
            c.request_write("buffer_top_target", 50)

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

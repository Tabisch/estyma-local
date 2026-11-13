"""Saved heating settings per pellet batch (synthetic values only)."""

import json
import os
import tempfile
import unittest

from estyma_local.batches import FILE_NAME, SnapshotError, SnapshotStore, settings_items
from estyma_local.controller import Controller


def controller_with(regs=None):
    ctl = Controller("hybrid", tempfile.mkdtemp())
    for (reg, idx), raw in (regs or {}).items():
        ctl.registers[(reg, idx)] = (1000.0, raw)
    return ctl


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = 1_800_000_000.0
        self.store = SnapshotStore(self.dir, clock=lambda: self.t)

    def test_needs_read_settings(self):
        with self.assertRaisesRegex(SnapshotError, "no settings read"):
            self.store.add(controller_with().settings_snapshot(), "Supplier A")

    def test_add_list_update_delete_persist(self):
        snap = controller_with({(8712, 1): 10, (8195, 1): 62}).settings_snapshot()
        with self.assertRaisesRegex(SnapshotError, "supplier is required"):
            self.store.add(snap, "  ")
        with self.assertRaisesRegex(SnapshotError, "YYYY-MM-DD"):
            self.store.add(snap, "Supplier A", "13.11.2026")
        a = self.store.add(snap, " Supplier A ", "2026-10-01", "6 mm")
        self.t += 10
        b = self.store.add(snap, "Supplier B", "2026-11-01")
        self.assertEqual(a["supplier"], "Supplier A")
        items = {i["key"]: i for i in a["settings"]}
        self.assertEqual(items["hyst_on"]["value"], 10)
        self.assertEqual(items["hyst_on"]["register"], "8712,1")
        self.assertIsNone(items["hyst_off"]["value"])
        self.assertEqual([s["id"] for s in self.store.list()], [b["id"], a["id"]])  # newest date first
        self.assertEqual(self.store.suppliers(), ["Supplier A", "Supplier B"])

        self.store.update(a["id"], day="2026-12-01", comment="more ash")
        again = SnapshotStore(self.dir)  # read back from the file
        first = again.list()[0]
        self.assertEqual((first["id"], first["comment"], first["date"]), (a["id"], "more ash", "2026-12-01"))
        self.assertEqual(first["settings"], a["settings"])  # settings never change
        with self.assertRaises(KeyError):
            again.update("nope", comment="x")
        again.delete(a["id"])
        self.assertEqual([s["id"] for s in SnapshotStore(self.dir).list()], [b["id"]])

    def test_default_date_is_today(self):
        from datetime import date
        s = self.store.add(controller_with({(8712, 1): 10}).settings_snapshot(), "X")
        self.assertEqual(s["date"], date.today().isoformat())

    def test_enum_keeps_option_name(self):
        ctl = controller_with()
        snap = ctl.settings_snapshot()
        enum = next(i for g in snap["groups"] for i in g["items"] if i["kind"] == "enum" and i["options"])
        value = next(iter(enum["options"]))
        enum["value"] = int(value)
        it = next(i for i in settings_items(snap) if i["key"] == enum["key"])
        self.assertEqual(it["option"], enum["options"][value])

    def test_broken_file_is_kept_aside(self):
        with open(os.path.join(self.dir, FILE_NAME), "w") as fh:
            fh.write("{not json")
        store = SnapshotStore(self.dir, clock=lambda: self.t)
        self.assertEqual(store.list(), [])
        self.assertTrue(any(n.startswith(FILE_NAME + ".broken-") for n in os.listdir(self.dir)))

    def test_file_is_not_a_log(self):
        self.store.add(controller_with({(8712, 1): 10}).settings_snapshot(), "X")
        self.assertFalse(FILE_NAME.endswith(".jsonl"))  # logs page / log retention only touch .jsonl
        with open(os.path.join(self.dir, FILE_NAME)) as fh:
            self.assertEqual(json.load(fh)["version"], 1)


if __name__ == "__main__":
    unittest.main()

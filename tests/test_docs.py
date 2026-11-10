"""The generated docs and the env var list must match the code."""

import os
import re
import unittest

from estyma_local import app
from estyma_local import catalog as C
from estyma_local import config as K
from estyma_local import controller
from estyma_local import gendocs
from estyma_local import monitor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE_DIRS = ("estyma_local", "estyma_local/analysis")
# env("X"), env_flag("X"), e("X"), flag("X"), env.get("X"), environ.get("X"), env["X"]
_READ = re.compile(r"""(?:\b(?:env|env_flag|e|flag)|\benv\.get|environ\.get|environ)\s*[\(\[]\s*["']([A-Z][A-Z0-9_]+)["']""")


def _source(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def _code_files():
    for d in CODE_DIRS:
        for name in sorted(os.listdir(os.path.join(ROOT, d))):
            if name.endswith(".py") and name != "config.py":
                yield os.path.join(d, name)


class GeneratedDocsTest(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(gendocs.stale(), [], "run: python3 -m estyma_local.gendocs")


class EnvVarsTest(unittest.TestCase):
    def test_code_reads_only_listed_variables(self):
        read = {}
        for path in _code_files():
            for name in _READ.findall(_source(path)):
                read.setdefault(name, path)
        unknown = {n: p for n, p in read.items() if n not in K.BY_NAME}
        self.assertEqual(unknown, {}, "add these to estyma_local/config.py")
        unused = set(K.BY_NAME) - set(read) - {e.name for e in K.ENV if e.section == "host"}
        self.assertEqual(unused, set(), "listed in estyma_local/config.py but never read")

    def test_app_defaults_come_from_config(self):
        cfg = app.load_config([], environ={"UPSTREAM": "http://203.0.113.10:80"})
        for name in ("LISTEN_PORT", "UPSTREAM_TIMEOUT", "UI_PORT", "MONITOR_HISTORY_HOURS",
                     "MODE", "CONTROLLER_TZ", "CLOCK_SYNC_HOURS", "POLL_DELAY", "WRITE_TIMEOUT",
                     "SETTINGS_READ_MINUTES", "WRITE_MIN_INTERVAL"):
            got = getattr(cfg, name.lower())
            self.assertEqual(str(got).removesuffix(".0"), K.BY_NAME[name].default, name)
        self.assertEqual(cfg.log_dir, "logs")   # outside the image; the image sets LOG_DIR=/logs
        self.assertIsNone(cfg.state_file)
        self.assertFalse(cfg.writes_enabled)

    def test_env_overrides_default(self):
        cfg = app.load_config([], environ={"MODE": "local", "WRITES_ENABLED": "1", "UI_PORT": "0"})
        self.assertEqual((cfg.mode, cfg.writes_enabled, cfg.ui_port), ("local", True, 0))

    def test_compose_passes_marked_variables(self):
        src = _source("docker-compose.yml")
        found = dict(re.findall(r"\$\{([A-Z_0-9]+):-([^}]*)\}", src))
        for e in K.ENV:
            if e.compose:
                self.assertIn(e.name, found, f"{e.name} missing in docker-compose.yml")
                self.assertEqual(found[e.name], e.default, f"{e.name} default in docker-compose.yml")
            else:
                self.assertNotIn(e.name, found, f"{e.name} is in docker-compose.yml: mark it compose=True")


class CatalogTest(unittest.TestCase):
    def test_monitor_fields_come_from_catalog(self):
        for key, label, reg, unit in monitor.LIVE + monitor.SINGLE_SETTINGS + monitor.SETTINGS:
            self.assertEqual((label, unit), (C.get(reg).label, C.get(reg).unit), key)
            self.assertIsNotNone(C.get(reg).plausible or C.get(reg).range, f"{key}: no valid range")

    def test_settings_ranges_within_catalog(self):
        for w in controller.WRITABLE.values():
            r = C.get(w.reg)
            if r.range:
                lo, hi = (round(v * r.scale) for v in r.range)
                self.assertTrue(lo <= w.lo <= w.hi <= hi, w.key)
            self.assertEqual(w.scale, r.scale, w.key)
            self.assertEqual(w.conf, r.conf, w.key)


if __name__ == "__main__":
    unittest.main()

"""The generated docs and the env var list must match the code."""

import os
import re
import sys
import unittest

import fixtures  # noqa: F401  (puts proxy/ on sys.path)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import catalog as C  # noqa: E402
import config as K  # noqa: E402
import controller  # noqa: E402
import gendocs  # noqa: E402
import monitor  # noqa: E402

CODE_DIRS = ("proxy", "compare")
# env("X"), env_flag("X"), env.get("X"), environ.get("X"), env["X"]
_READ = re.compile(r"""(?:\benv(?:_flag)?|\benv\.get|environ\.get|environ)\s*[\(\[]\s*["']([A-Z][A-Z0-9_]+)["']""")


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
        self.assertEqual(gendocs.stale(), [], "run: python3 tools/gendocs.py")


class EnvVarsTest(unittest.TestCase):
    def test_code_reads_only_listed_variables(self):
        read = {}
        for path in _code_files():
            for name in _READ.findall(_source(path)):
                read.setdefault(name, path)
        unknown = {n: p for n, p in read.items() if n not in K.BY_NAME}
        self.assertEqual(unknown, {}, "add these to proxy/config.py")
        unused = set(K.BY_NAME) - set(read) - {e.name for e in K.ENV if e.section == "host"}
        self.assertEqual(unused, set(), "listed in proxy/config.py but never read")

    def test_proxy_defaults_match(self):
        src = _source("proxy/estyma_proxy.py")
        for name, default in re.findall(r'env\("([A-Z_]+)", "([^"]*)"\)', src):
            documented = K.BY_NAME[name].default
            if name in ("LOG_DIR",):  # "logs" outside the image, the image sets /logs
                continue
            self.assertEqual(default, documented, name)

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
        for key, label, reg, idx, how, unit, lo, hi in monitor.LIVE + monitor.SINGLE_SETTINGS:
            self.assertEqual((label, unit), (C.get(reg).label, C.get(reg).unit), key)

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

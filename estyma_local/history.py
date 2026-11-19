"""Compact register history: only the values that changed, in SQLite.

The controller reports ~190 register values every ~20 s, but only a few
change between two reports (about 13,000 changes a day against 250,000
values). ``HistoryStore`` keeps exactly those changes in
``LOG_DIR/history.sqlite3``, about 0.5 MB a day, so the history survives a
restart without the raw exchange logs (which are off by default, see
``raw_logs.py``).

Table ``vals(t, reg, idx, val)``: the raw value of register ``reg`` (index
``idx``) as reported at Unix time ``t``, written when it differs from the
last value written for that key. Once an hour (``KEYFRAME_S``) every reported
value is written again, so a key that is no longer reported can be told
apart from one that does not change: during replay a key counts as gone when
nothing was written for it for ``STALE_S``. Values are stored raw, before the
glitch rule in decode.py, so replaying gives the readers exactly what the
controller sent.

Table ``cloud_fuel(day, kg, device, imported)``: fuel burnt per calendar day
as the Estyma cloud's statistics show it, imported once on request
(cloud_fuel_import.py). Kept apart from ``vals`` so imported numbers are
never mixed up with the controller's own readings; one row per day, so
importing again replaces instead of adding."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime

from .protocol import loose_form, parse_report

FILENAME = "history.sqlite3"
KEYFRAME_S = 3600
STALE_S = KEYFRAME_S + 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS vals (
    t INTEGER NOT NULL, reg INTEGER NOT NULL, idx INTEGER NOT NULL, val INTEGER NOT NULL,
    PRIMARY KEY (t, reg, idx)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS cloud_fuel (
    day TEXT PRIMARY KEY, kg REAL NOT NULL, device TEXT, imported REAL NOT NULL
);
"""


def report_of(rec: dict) -> tuple[float, dict] | None:
    """(time, {(reg, idx): raw}) of a logged controller report, else None.
    Skips the records the monitor skips too (cloud mirror, late replies)."""
    req = rec.get("request") or {}
    if req.get("path") != "/device/connect" or rec.get("mirror") or rec.get("late_for"):
        return None
    body = req.get("body") or {}
    form = loose_form(body.get("text") or "")
    regs = parse_report(form.get("ZAPISZ_ODCZYT", "") or "")
    if not regs:
        return None
    try:
        ts = datetime.fromisoformat(rec["time"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return ts, regs


class _Dedup:
    """Which values to write: changed ones, and all of them once an hour."""

    def __init__(self) -> None:
        self.last: dict = {}
        self.keyframe = None

    def rows(self, t: int, regs: dict) -> list[tuple]:
        full = self.keyframe is None or t - self.keyframe >= KEYFRAME_S or t < self.keyframe
        if full:
            self.keyframe = t
        out = []
        for (reg, idx), val in regs.items():
            if full or self.last.get((reg, idx)) != val:
                self.last[(reg, idx)] = val
                out.append((t, reg, idx, val))
        return out


class HistoryStore:
    def __init__(self, log_dir: str, filename: str = FILENAME, clock=time.time) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, filename)
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._live = _Dedup()
        self.last_error = None
        self.cloud_version = 0   # bumped on every cloud_fuel change, for FuelStats

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writing ------------------------------------------------------------

    def _insert(self, rows) -> None:
        if not rows:
            return
        with self._lock, self._db:
            self._db.executemany("INSERT OR IGNORE INTO vals VALUES (?, ?, ?, ?)", rows)

    def record(self, ts: float, regs: dict) -> int:
        """Store one live report; returns the number of values written."""
        rows = self._live.rows(int(ts), regs)
        self._insert(rows)
        return len(rows)

    def ingest(self, rec: dict) -> None:
        """ExchangeLog listener: store the report in a logged exchange."""
        try:
            got = report_of(rec)
            if got:
                self.record(*got)
            self.last_error = None
        except Exception as exc:  # never break the proxy because of the store
            if self.last_error is None:
                print(f"history store: {type(exc).__name__}: {exc}", flush=True)
            self.last_error = f"{type(exc).__name__}: {exc}"

    # -- reading ------------------------------------------------------------

    def replay(self, since: float, until: float | None = None):
        """Yield (t, {(reg, idx): raw}) at every time a value was written,
        from ``since`` on, each with all values known at that moment (like
        one controller report)."""
        since, until = int(since), int(until if until is not None else 2**62)
        with self._lock:
            start = self._db.execute(
                "SELECT reg, idx, val, max(t) FROM vals WHERE t < ? AND t >= ? GROUP BY reg, idx",
                (since, since - STALE_S)).fetchall()
            rows = self._db.execute(
                "SELECT t, reg, idx, val FROM vals WHERE t >= ? AND t < ? ORDER BY t",
                (since, until)).fetchall()
        state = {(reg, idx): (t, val) for reg, idx, val, t in start}
        i = 0
        while i < len(rows):
            t = rows[i][0]
            while i < len(rows) and rows[i][0] == t:
                _, reg, idx, val = rows[i]
                state[(reg, idx)] = (t, val)
                i += 1
            yield t, {k: v for k, (seen, v) in state.items() if t - seen <= STALE_S}

    def series(self, reg: int, idx: int = 1, since: float = 0, until: float | None = None):
        """[(t, raw)] of one register, changes only."""
        with self._lock:
            return self._db.execute(
                "SELECT t, val FROM vals WHERE reg = ? AND idx = ? AND t >= ? AND t < ? ORDER BY t",
                (reg, idx, int(since), int(until if until is not None else 2**62))).fetchall()

    # -- imported cloud fuel statistics ----------------------------------------

    def put_cloud_fuel(self, days: dict, device: str | None = None) -> int:
        """Store {"YYYY-MM-DD": kg}; a day already there is replaced."""
        now = self.clock()
        rows = [(d, float(kg), device, now) for d, kg in days.items()]
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO cloud_fuel VALUES (?, ?, ?, ?)", rows)
        self.cloud_version += 1
        return len(rows)

    def cloud_fuel(self) -> dict:
        """{"YYYY-MM-DD": kg} of every imported day."""
        with self._lock:
            return dict(self._db.execute("SELECT day, kg FROM cloud_fuel ORDER BY day").fetchall())

    def cloud_fuel_status(self) -> dict:
        with self._lock:
            n, first, last, imported, kg = self._db.execute(
                "SELECT count(*), min(day), max(day), max(imported), sum(kg) FROM cloud_fuel").fetchone()
        return {"days": n, "first": first, "last": last, "imported": imported,
                "kg": round(kg, 1) if kg is not None else None}

    def clear_cloud_fuel(self) -> int:
        with self._lock, self._db:
            n = self._db.execute("DELETE FROM cloud_fuel").rowcount
        self.cloud_version += 1
        return n

    def status(self) -> dict:
        with self._lock:
            first, last, n = self._db.execute("SELECT min(t), max(t), count(*) FROM vals").fetchone()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = None
        return {"file": os.path.basename(self.path), "size": size, "first": first, "last": last,
                "values": n, "error": self.last_error}

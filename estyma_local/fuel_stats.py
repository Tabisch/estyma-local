"""Fuel consumption per day, week, month and year, from the fuel counter.

The controller reports its total fuel counter (8484, kg) in every report.
The history store (history.py) keeps every change of it, so the fuel burnt
in any period is the sum of the counter's increases inside that period.
``FuelStats`` reads those changes once, keeps them in memory (a few
thousand steps a year) and picks up new ones on every query.

Periods are calendar periods in the controller's time zone
(``CONTROLLER_TZ``): days from midnight, weeks from Monday.

Bad readings: a single corrupted read (lower than the last good value, or a
jump larger than the boiler can burn in the time since) is ignored. If the
implausible value stays for ``CONFIRM_S`` (the counter was reset or set by
hand on the controller), it becomes the new base without counting as
consumption.

Gaps: the store has a reading at least every hour (keyframes). When the
server was not recording for longer than ``GAP_S``, the fuel burnt in the
gap is spread evenly over it, and the periods that touch a gap are marked
``estimated``.

Cost: with ``prices`` (a callable returning ``(date, price per tonne,
supplier)`` oldest first, SnapshotStore.prices), the fuel burnt on a day
costs the price of the last priced pellet batch dated on or before that
day, from local midnight. Fuel burnt before the first priced batch has no
price: a period with some of it is marked ``cost_partial`` (its cost counts
the priced part only), one with nothing priced has ``cost`` None.
"""

from __future__ import annotations

import bisect
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo

from . import decode as D

REG = 8484
MAX_KG_PER_HOUR = 20.0     # well above the burner's maximum (~3 kg/h)
MAX_JUMP_KG = 50.0
CONFIRM_S = 600
GAP_S = 2 * 3600
PERIODS = ("day", "week", "month", "year")
DEFAULT_COUNT = {"day": 31, "week": 12, "month": 12, "year": 5}
MAX_COUNT = {"day": 400, "week": 104, "month": 60, "year": 20}
UNPRICED_KG = 0.05         # less unpriced fuel than this does not make a cost partial


DEFAULT_KWH_PER_KG = 4.8   # typical wood pellets


def calorific_value(ctl=None, env=os.environ) -> float:
    """kWh per kg: FUEL_KWH_PER_KG, else the controller's setting (8850,
    raw 20..100 = 2.0..10.0), else 4.8."""
    try:
        v = float((env.get("FUEL_KWH_PER_KG") or "").strip())
        if v > 0:
            return v
    except ValueError:
        pass
    cur = ctl.current("calorific_value") if ctl is not None else None
    if cur is not None and 20 <= cur <= 100:
        return cur / 10
    return DEFAULT_KWH_PER_KG


def period_start(d: date, period: str) -> date:
    if period == "day":
        return d
    if period == "week":
        return d - timedelta(days=d.weekday())
    if period == "month":
        return d.replace(day=1)
    if period == "year":
        return d.replace(month=1, day=1)
    raise ValueError(f"unknown period {period!r}")


def next_start(d: date, period: str) -> date:
    if period == "day":
        return d + timedelta(days=1)
    if period == "week":
        return d + timedelta(days=7)
    if period == "month":
        return date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return date(d.year + 1, 1, 1)


def prev_start(d: date, period: str) -> date:
    return period_start(d - timedelta(days=1), period)


class FuelStats:
    def __init__(self, store, tz: tzinfo | None = None, clock=time.time, prices=None,
                 currency: str = "EUR") -> None:
        self.store = store
        self.prices = prices          # callable -> [(date iso, price per tonne, supplier)]
        self.currency = currency
        self.tz = tz or timezone.utc
        self.clock = clock
        self._lock = threading.Lock()
        self._read_until = 0          # store rows before this time are read
        self._last: tuple | None = None      # (t, kg) last accepted reading
        self._pending: tuple | None = None   # (t, kg) implausible reading being checked
        # Counter increases as segments (a, b, kg burnt before, kg): a point
        # (a == b) normally, the whole gap [a, b] after a recording gap.
        self._seg: list = []
        self._end: list = []          # b of each segment, for bisect
        self._gaps: list = []         # (a, b) of the spread segments
        self.first: int | None = None  # time of the first usable reading

    # -- feeding ------------------------------------------------------------

    def _feed(self, t: int, kg: float) -> None:
        if self.first is None:
            self.first = t
        if self._last is None:
            self._last = (t, kg)
            return
        last_t, last = self._last
        limit = MAX_JUMP_KG + MAX_KG_PER_HOUR * max(0, t - last_t) / 3600
        if 0 <= kg - last <= limit:
            self._pending = None
            if kg > last:
                before = self._seg[-1][2] + self._seg[-1][3] if self._seg else 0.0
                a = last_t if t - last_t > GAP_S else t
                self._seg.append((a, t, before, kg - last))
                self._end.append(t)
                if a < t:
                    self._gaps.append((a, t))
            self._last = (t, kg)
            return
        # Implausible: a glitch unless it stays (counter reset / set by hand).
        if self._pending is None or abs(kg - self._pending[1]) > MAX_JUMP_KG:
            self._pending = (t, kg)
        elif t - self._pending[0] >= CONFIRM_S:
            self._last = (t, kg)
            self._pending = None

    def refresh(self) -> None:
        """Read the counter changes stored since the last call."""
        if self.store is None:
            return
        with self._lock:
            rows = self.store.series(REG, 1, since=self._read_until)
            for t, raw in rows:
                kg = D.value(REG, raw)
                if kg is not None:
                    self._feed(t, kg)
            if rows:
                self._read_until = rows[-1][0] + 1

    # -- reading ------------------------------------------------------------

    def _burnt_before(self, x: float) -> float:
        i = bisect.bisect_left(self._end, x)   # segments before i end before x
        if i == len(self._seg):
            return self._seg[-1][2] + self._seg[-1][3] if self._seg else 0.0
        a, b, before, kg = self._seg[i]
        if a < x:                              # x inside a spread gap
            before += kg * (x - a) / (b - a)
        return before

    def _estimated(self, a: float, b: float) -> bool:
        return any(ga < b and a < gb for ga, gb in self._gaps)

    def between(self, a: float, b: float) -> float:
        """kg burnt in [a, b) (Unix times)."""
        with self._lock:
            return round(self._burnt_before(b) - self._burnt_before(a), 1)

    def _price_steps(self) -> list:
        """[(from Unix time, price per tonne, supplier)], oldest first."""
        if self.prices is None:
            return []
        out = []
        try:
            rows = self.prices()
        except Exception as exc:  # never break the fuel statistics
            print(f"fuel stats: cannot read batch prices: {type(exc).__name__}: {exc}", flush=True)
            return []
        for day, price, supplier in rows:
            try:
                out.append((self._ts(date.fromisoformat(day)), float(price), supplier))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
        return out

    def _cost(self, a: float, b: float, steps: list) -> tuple:
        """(cost, kg without a price) of the fuel burnt in [a, b); the lock
        must be held. cost is None when no part of [a, b) has a price."""
        cost, unpriced, priced = 0.0, 0.0, False
        times = [t for t, _, _ in steps]
        cuts = [a, *(t for t in times if a < t < b), b]
        for x, y in zip(cuts, cuts[1:]):
            kg = self._burnt_before(y) - self._burnt_before(x)
            i = bisect.bisect_right(times, x) - 1
            if i < 0:
                unpriced += kg
            else:
                priced = True
                cost += kg * steps[i][1] / 1000
        return (round(cost, 2) if priced else None), unpriced

    def cost_between(self, a: float, b: float) -> dict:
        """{cost, partial} for the fuel burnt in [a, b)."""
        steps = self._price_steps()
        with self._lock:
            cost, unpriced = self._cost(a, b, steps)
        return {"cost": cost, "partial": cost is not None and unpriced >= UNPRICED_KG}

    def price_at(self, t: float | None = None) -> dict | None:
        """The batch price in effect at ``t`` (default now)."""
        t = self.clock() if t is None else t
        cur = None
        for start, price, supplier in self._price_steps():
            if start <= t:
                cur = {"price": price, "supplier": supplier,
                       "since": datetime.fromtimestamp(start, self.tz).date().isoformat()}
        return cur

    def _ts(self, d: date) -> float:
        return datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp()

    def today(self, now: float | None = None) -> date:
        now = self.clock() if now is None else now
        return datetime.fromtimestamp(now, self.tz).date()

    def buckets(self, period: str, count: int | None = None, now: float | None = None) -> list:
        """The last ``count`` periods, oldest first: {start, end, kg}; the
        newest one is the running period. kg is None before the first reading."""
        if period not in PERIODS:
            raise ValueError(f"unknown period {period!r}")
        count = max(1, min(int(count or DEFAULT_COUNT[period]), MAX_COUNT[period]))
        self.refresh()
        start = period_start(self.today(now), period)
        starts = [start]
        for _ in range(count - 1):
            starts.append(prev_start(starts[-1], period))
        steps = self._price_steps()
        out = []
        for s in reversed(starts):
            e = next_start(s, period)
            a, b = self._ts(s), self._ts(e)
            covered = self.first is not None and b > self.first
            with self._lock:
                est = self._estimated(a, b)
                cost, unpriced = self._cost(a, b, steps) if covered else (None, 0.0)
            out.append({"start": s.isoformat(), "end": e.isoformat(),
                        "kg": self.between(a, b) if covered else None, "estimated": est,
                        "cost": cost, "cost_partial": cost is not None and unpriced >= UNPRICED_KG})
        return out

    def _summary_periods(self, now: float | None):
        """(key, start, end) of today, yesterday, this and last week, month, year."""
        d = self.today(now)
        for key, period, back in (("today", "day", 0), ("yesterday", "day", 1),
                                  ("week", "week", 0), ("last_week", "week", 1),
                                  ("month", "month", 0), ("last_month", "month", 1),
                                  ("year", "year", 0), ("last_year", "year", 1)):
            s = period_start(d, period)
            for _ in range(back):
                s = prev_start(s, period)
            yield key, self._ts(s), self._ts(next_start(s, period))

    def summary(self, now: float | None = None) -> dict:
        """kg today, yesterday, this week, this month and this year."""
        self.refresh()
        return {key: (self.between(a, b) if self.first is not None and b > self.first else None)
                for key, a, b in self._summary_periods(now)}

    def cost_summary(self, now: float | None = None) -> dict:
        """Like ``summary``, the cost of that fuel: {key: {cost, partial}}."""
        self.refresh()
        out = {}
        for key, a, b in self._summary_periods(now):
            out[key] = (self.cost_between(a, b) if self.first is not None and b > self.first
                        else {"cost": None, "partial": False})
        return out

    def snapshot(self, period: str = "day", count: int | None = None,
                 now: float | None = None, kwh_per_kg: float | None = None) -> dict:
        """What /api/fuel returns."""
        buckets = self.buckets(period, count, now)
        with self._lock:
            counter = self._last[1] if self._last else None
        return {"period": period, "tz": str(self.tz), "first": self.first, "counter": counter,
                "kwh_per_kg": kwh_per_kg, "buckets": buckets, "summary": self.summary(now),
                "currency": self.currency, "price": self.price_at(now),
                "cost_summary": self.cost_summary(now)}

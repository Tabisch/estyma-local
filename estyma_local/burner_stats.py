"""Burner statistics per day, week, month and year, from the burner state.

The controller reports the burner state (8199) in every report; the history
store (history.py) keeps every change of it. From that sequence:

* **starts**: every time the burner enters *ignition* (1),
* **failed starts**: an ignition that ends (cleaning, burn-out, off, error
  or a new ignition) before the flame was established, i.e. before
  *glowing* (2), *modulation* (5) or *sustain* (8). Seen in the captures as
  ignition -> burn-out -> cleaning -> ignition again,
* **error stops**: every time the burner enters *error* (10),
* **burner hours**: time with a flame, in glowing, modulation or sustain,
* **average burn**: burner hours divided by the successful starts.

Many short burns (lots of starts, short average burn) is the classic sign
of a badly tuned pellet boiler (hysteresis, power), so these numbers help to
compare settings between pellet batches.

Periods are calendar periods in the controller's time zone (days from
midnight, weeks from Monday), like fuel_stats.py. The store writes every
value at least once an hour; when nothing was recorded for longer than
``history.STALE_S`` the time in between is unknown and not counted, and the
periods that touch such a gap are marked ``estimated``. An ignition whose
outcome fell into a gap counts as a start but neither as failed nor as
successful.
"""

from __future__ import annotations

import bisect
import threading
import time
from datetime import datetime, timezone, tzinfo

from . import decode as D
from .fuel_stats import DEFAULT_COUNT, MAX_COUNT, PERIODS, next_start, period_start, prev_start
from .history import STALE_S

REG = 8199
IGNITION, ERROR = 1, 10
FLAME = frozenset((2, 5, 8))            # glowing, modulation, sustain


class BurnerStats:
    def __init__(self, store, tz: tzinfo | None = None, clock=time.time) -> None:
        self.store = store
        self.tz = tz or timezone.utc
        self.clock = clock
        self._lock = threading.Lock()
        self._read_until = 0
        self.first: int | None = None    # time of the first usable reading
        # Closed flame intervals (a, b) and their ends for bisect.
        self._flame: list = []
        self._flame_end: list = []
        # Events: ignitions [t, outcome] (outcome True / False / None), errors [t].
        self._ign_t: list = []
        self._ign_ok: list = []
        self._err_t: list = []
        self._gaps: list = []            # (a, b) without readings
        self._state: int | None = None   # current state, since _since, last seen _seen
        self._since = 0
        self._seen = 0
        self._open: int | None = None    # index of the ignition waiting for its outcome

    # -- feeding ------------------------------------------------------------

    def _close(self, end: int) -> None:
        """End the current state at ``end``."""
        if self._state in FLAME and end > self._since:
            self._flame.append((self._since, end))
            self._flame_end.append(end)

    def _feed(self, t: int, s: int) -> None:
        if self.first is None:
            self.first = t
        if self._state is not None and t - self._seen > STALE_S:
            # Nothing recorded for a while: what happened in between is unknown.
            self._close(self._seen)
            self._gaps.append((self._seen, t))
            self._open = None
            self._state, self._since = s, t   # an ignition already running is not counted
            self._seen = t
            return
        self._seen = t
        if s == self._state:
            return
        prev = self._state
        if prev is not None:
            self._close(t)
        self._state, self._since = s, t
        if self._open is not None:
            if s in FLAME:
                self._ign_ok[self._open] = True
                self._open = None
            else:                          # left ignition without a flame
                self._ign_ok[self._open] = False
                self._open = None
        if s == IGNITION and prev is not None:
            self._ignition(t)
        if s == ERROR and prev is not None:
            self._err_t.append(t)

    def _ignition(self, t: int) -> None:
        self._ign_t.append(t)
        self._ign_ok.append(None)
        self._open = len(self._ign_t) - 1

    def refresh(self) -> None:
        if self.store is None:
            return
        with self._lock:
            rows = self.store.series(REG, 1, since=self._read_until)
            for t, raw in rows:
                v = D.value(REG, raw)
                if v is not None:
                    self._feed(int(t), int(v))
            if rows:
                self._read_until = rows[-1][0] + 1

    # -- reading ------------------------------------------------------------

    def _flame_seconds(self, a: float, b: float, now: float) -> float:
        total = 0.0
        i = bisect.bisect_right(self._flame_end, a)
        while i < len(self._flame) and self._flame[i][0] < b:
            fa, fb = self._flame[i]
            total += max(0.0, min(b, fb) - max(a, fa))
            i += 1
        if self._state in FLAME:          # still burning
            end = min(now, self._seen + STALE_S)
            total += max(0.0, min(b, end) - max(a, self._since))
        return total

    def _count(self, times: list, a: float, b: float) -> tuple[int, int]:
        return bisect.bisect_left(times, a), bisect.bisect_left(times, b)

    def _estimated(self, a: float, b: float, now: float) -> bool:
        if any(ga < b and a < gb for ga, gb in self._gaps):
            return True
        # Not reporting right now (the open period runs past the last reading).
        return self._state is not None and now - self._seen > STALE_S and self._seen < b and a < now

    def period(self, a: float, b: float, now: float | None = None) -> dict:
        """Statistics of [a, b) (Unix times)."""
        now = self.clock() if now is None else now
        self.refresh()
        with self._lock:
            i, j = self._count(self._ign_t, a, b)
            ok = self._ign_ok[i:j]
            e0, e1 = self._count(self._err_t, a, b)
            secs = self._flame_seconds(a, b, now)
            est = self._estimated(a, b, now)
        good = sum(1 for x in ok if x)
        return {"starts": j - i, "failed": sum(1 for x in ok if x is False), "errors": e1 - e0,
                "hours": round(secs / 3600, 2),
                "avg_burn_min": round(secs / 60 / good, 1) if good else None,
                "estimated": est}

    def _ts(self, d) -> float:
        return datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp()

    def today(self, now: float | None = None):
        now = self.clock() if now is None else now
        return datetime.fromtimestamp(now, self.tz).date()

    def _covered(self, b: float) -> bool:
        return self.first is not None and b > self.first

    def buckets(self, period: str, count: int | None = None, now: float | None = None) -> list:
        """The last ``count`` periods, oldest first; the newest is the running one.
        The numbers are None before the first reading."""
        if period not in PERIODS:
            raise ValueError(f"unknown period {period!r}")
        count = max(1, min(int(count or DEFAULT_COUNT[period]), MAX_COUNT[period]))
        now = self.clock() if now is None else now
        self.refresh()
        starts = [period_start(self.today(now), period)]
        for _ in range(count - 1):
            starts.append(prev_start(starts[-1], period))
        out = []
        for s in reversed(starts):
            e = next_start(s, period)
            a, b = self._ts(s), self._ts(e)
            row = {"start": s.isoformat(), "end": e.isoformat()}
            if self._covered(b):
                row.update(self.period(a, b, now))
            else:
                row.update(starts=None, failed=None, errors=None, hours=None, avg_burn_min=None,
                           estimated=False)
            out.append(row)
        return out

    def summary(self, now: float | None = None) -> dict:
        """Today, yesterday, this week, this month and this year."""
        now = self.clock() if now is None else now
        self.refresh()
        d = self.today(now)
        out = {}
        for key, period, back in (("today", "day", 0), ("yesterday", "day", 1), ("week", "week", 0),
                                  ("month", "month", 0), ("year", "year", 0)):
            s = period_start(d, period)
            for _ in range(back):
                s = prev_start(s, period)
            a, b = self._ts(s), self._ts(next_start(s, period))
            out[key] = self.period(a, b, now) if self._covered(b) else None
        return out

    def snapshot(self, period: str = "day", count: int | None = None,
                 now: float | None = None) -> dict:
        """What /api/burner returns."""
        buckets = self.buckets(period, count, now)
        with self._lock:
            state = self._state
        return {"period": period, "tz": str(self.tz), "first": self.first, "state": state,
                "buckets": buckets, "summary": self.summary(now)}

"""Saved heating settings per pellet batch (the ``/batches`` page).

Different pellet suppliers burn differently, so the burner settings get
tuned for each batch. A *snapshot* keeps the heating settings as shown on
``/heating`` at that moment, together with the supplier, a date (default
today) and a free comment. Read only: nothing here writes to the
controller.

Snapshots are kept in ``settings-snapshots.json`` in LOG_DIR, next to
``state.json``. It is not a ``.jsonl`` log, so neither the logs page nor
the automatic log deletion removes it. Each snapshot stores the labels,
units and option names as they were, so it still reads the same after the
catalog changes.

A snapshot can carry the batch's price per tonne (``price``, in
``CURRENCY``). The fuel statistics (fuel_stats.py) use the price of the
batch in effect on each day for the cost of the fuel burnt, see
``SnapshotStore.prices``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import date

from .controller import WRITABLE

FILE_NAME = "settings-snapshots.json"
MAX_SNAPSHOTS = 2000
MAX_SUPPLIER = 100
MAX_COMMENT = 4000
MAX_PRICE = 100000.0
CURRENCY = "EUR"
KEEP = object()   # update(): leave the value as it is


class SnapshotError(ValueError):
    """Bad input; the message is shown on the page."""


def _clean_date(value) -> str:
    if value in (None, ""):
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise SnapshotError("date must be YYYY-MM-DD") from None


def _clean_text(value, name: str, limit: int, required: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise SnapshotError(f"{name} is required")
    if len(text) > limit:
        raise SnapshotError(f"{name} is too long (max {limit} characters)")
    return text


def _clean_price(value) -> float | None:
    """Price per tonne, or None for no price. Accepts a decimal comma."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise SnapshotError("price must be a number")
    try:
        price = float(str(value).strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise SnapshotError("price must be a number") from None
    if not 0 <= price <= MAX_PRICE:
        raise SnapshotError(f"price must be between 0 and {MAX_PRICE:.0f}")
    return round(price, 2)


def settings_items(settings_snapshot: dict) -> list[dict]:
    """The settings of ``Controller.settings_snapshot()`` in the form kept in
    a snapshot: what was shown, not what can be written."""
    out = []
    for g in settings_snapshot.get("groups", []):
        for it in g.get("items", []):
            value = it.get("value")
            opt = None
            if value is not None:
                # the page's options are narrowed by the current limits; fall
                # back to the full list for a value outside them
                opt = (it.get("options") or {}).get(str(value))
                w = WRITABLE.get(it["key"])
                if opt is None and w is not None and w.options and value == int(value):
                    opt = w.options.get(int(value))
            out.append({
                "key": it["key"], "group": g["key"],
                "group_label": g.get("label", ""), "group_label_de": g.get("label_de", ""),
                "label": it.get("label", ""), "label_de": it.get("label_de", ""),
                "register": it.get("register"), "unit": it.get("unit", ""), "kind": it.get("kind"), "step": it.get("step"),
                "value": value, "raw": it.get("raw"), "seen": it.get("seen"),
                "option": list(opt) if opt else None,
            })
    return out


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


class SnapshotStore:
    """Thread safe list of snapshots in one JSON file."""

    def __init__(self, log_dir: str, clock=time.time):
        self.path = os.path.join(log_dir, FILE_NAME)
        self.clock = clock
        self._lock = threading.Lock()
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # Keep the broken file for a look by hand; never overwrite it.
            broken = f"{self.path}.broken-{int(self.clock())}"
            print(f"settings snapshots: cannot read {self.path} ({exc}), moved to {broken}", flush=True)
            try:
                os.replace(self.path, broken)
            except OSError:
                pass
            return []
        items = data.get("snapshots") if isinstance(data, dict) else None
        return [s for s in items if isinstance(s, dict) and "id" in s] if isinstance(items, list) else []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "snapshots": self._items}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # -- API ----------------------------------------------------------------

    def list(self) -> list[dict]:
        """All snapshots, newest date first (then newest saved)."""
        with self._lock:
            items = [dict(s) for s in self._items]
        return sorted(items, key=lambda s: (s.get("date", ""), s.get("created", 0)), reverse=True)

    def latest(self) -> dict | None:
        """The snapshot saved last (by save time, not by its date), for the
        "changed since the last save" marks on ``/heating``: its id, when
        and for which supplier, and the saved value per setting key."""
        with self._lock:
            snap = max(self._items, key=lambda s: s.get("created") or 0, default=None)
            if snap is None:
                return None
            return {
                "id": snap["id"], "created": snap.get("created"), "date": snap.get("date"),
                "supplier": snap.get("supplier", ""),
                "values": {it["key"]: it.get("value") for it in snap.get("settings", [])
                           if isinstance(it, dict) and "key" in it},
            }

    def matching(self, settings_snapshot: dict) -> list[dict]:
        """Snapshots whose settings all equal the current ones, last saved
        first. Only settings that have a value both now and in the snapshot
        are compared; a snapshot with none in common does not match."""
        now = {it["key"]: it.get("value") for g in settings_snapshot.get("groups", [])
               for it in g.get("items", []) if it.get("value") is not None}
        out = []
        with self._lock:
            snaps = sorted(self._items, key=lambda s: s.get("created") or 0, reverse=True)
            for snap in snaps:
                common = 0
                for it in snap.get("settings", []):
                    if not isinstance(it, dict) or it.get("value") is None or it.get("key") not in now:
                        continue
                    if not _same(it["value"], now[it["key"]]):
                        break
                    common += 1
                else:
                    if common:
                        out.append({"id": snap["id"], "created": snap.get("created"),
                                    "date": snap.get("date"), "supplier": snap.get("supplier", "")})
        return out

    def suppliers(self) -> list[str]:
        with self._lock:
            names = {s.get("supplier", "") for s in self._items}
        return sorted((n for n in names if n), key=str.lower)

    def prices(self) -> list[tuple[str, float, str]]:
        """(date, price per tonne, supplier) of the batches with a price,
        oldest first. A price holds from its batch's date until the date of
        the next batch with a price; batches without a price (say a second
        save after tuning the settings) keep the price in effect. Of several
        priced batches on one day the one saved last wins."""
        with self._lock:
            items = [(s.get("date", ""), s.get("created") or 0, s.get("price"), s.get("supplier", ""))
                     for s in self._items if isinstance(s.get("price"), (int, float))]
        items.sort(key=lambda x: (x[0], x[1]))
        out: list = []
        for day, _created, price, supplier in items:
            if out and out[-1][0] == day:
                out[-1] = (day, float(price), supplier)
            else:
                out.append((day, float(price), supplier))
        return out

    def add(self, settings_snapshot: dict, supplier, day=None, comment=None, price=None) -> dict:
        supplier = _clean_text(supplier, "supplier", MAX_SUPPLIER, required=True)
        price = _clean_price(price)
        comment = _clean_text(comment, "comment", MAX_COMMENT)
        day = _clean_date(day)
        items = settings_items(settings_snapshot)
        if not any(it["value"] is not None for it in items):
            raise SnapshotError("no settings read yet, read the settings first")
        snap = {
            "id": uuid.uuid4().hex[:12], "created": self.clock(), "updated": None,
            "date": day, "supplier": supplier, "comment": comment, "price": price,
            "mode": settings_snapshot.get("mode"),
            "settings_read": (settings_snapshot.get("settings") or {}).get("read"),
            "settings": items,
        }
        with self._lock:
            if len(self._items) >= MAX_SNAPSHOTS:
                raise SnapshotError(f"too many snapshots (max {MAX_SNAPSHOTS}), delete old ones first")
            self._items.append(snap)
            try:
                self._save()
            except OSError as exc:
                self._items.pop()
                raise SnapshotError(f"cannot save: {exc.strerror or exc}") from None
        print(f"settings snapshot saved: {snap['date']} {snap['supplier']!r}", flush=True)
        return snap

    def update(self, snap_id: str, supplier=None, day=None, comment=None, price=KEEP) -> dict:
        """Change supplier, date, comment or price (None or "" removes it);
        the settings stay as saved."""
        if price is not KEEP:
            price = _clean_price(price)
        with self._lock:
            snap = next((s for s in self._items if s["id"] == snap_id), None)
            if snap is None:
                raise KeyError(snap_id)
            new = dict(snap)
            if supplier is not None:
                new["supplier"] = _clean_text(supplier, "supplier", MAX_SUPPLIER, required=True)
            if day is not None:
                new["date"] = _clean_date(day)
            if comment is not None:
                new["comment"] = _clean_text(comment, "comment", MAX_COMMENT)
            if price is not KEEP:
                new["price"] = price
            new["updated"] = self.clock()
            i = self._items.index(snap)
            self._items[i] = new
            try:
                self._save()
            except OSError as exc:
                self._items[i] = snap
                raise SnapshotError(f"cannot save: {exc.strerror or exc}") from None
            return dict(new)

    def delete(self, snap_id: str) -> None:
        with self._lock:
            snap = next((s for s in self._items if s["id"] == snap_id), None)
            if snap is None:
                raise KeyError(snap_id)
            i = self._items.index(snap)
            del self._items[i]
            try:
                self._save()
            except OSError as exc:
                self._items.insert(i, snap)
                raise SnapshotError(f"cannot save: {exc.strerror or exc}") from None
        print(f"settings snapshot deleted: {snap.get('date')} {snap.get('supplier')!r}", flush=True)

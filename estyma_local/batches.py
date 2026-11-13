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

    def suppliers(self) -> list[str]:
        with self._lock:
            names = {s.get("supplier", "") for s in self._items}
        return sorted((n for n in names if n), key=str.lower)

    def add(self, settings_snapshot: dict, supplier, day=None, comment=None) -> dict:
        supplier = _clean_text(supplier, "supplier", MAX_SUPPLIER, required=True)
        comment = _clean_text(comment, "comment", MAX_COMMENT)
        day = _clean_date(day)
        items = settings_items(settings_snapshot)
        if not any(it["value"] is not None for it in items):
            raise SnapshotError("no settings read yet, read the settings first")
        snap = {
            "id": uuid.uuid4().hex[:12], "created": self.clock(), "updated": None,
            "date": day, "supplier": supplier, "comment": comment,
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

    def update(self, snap_id: str, supplier=None, day=None, comment=None) -> dict:
        """Change supplier, date or comment; the settings stay as saved."""
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

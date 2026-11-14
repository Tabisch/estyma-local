"""Delete old log files automatically, switched on the app settings page.

Every ``.jsonl`` file directly inside LOG_DIR (``exchanges-*``, ``cloud-*``,
``writes-*``) counts as a log. Once the setting is on, a file whose last
change is more than ``days`` days ago is deleted; the logs roll over daily,
so today's files are never touched. Other files (``state.json``,
``registers.json``, ``cloud-monitor.json``, this module's own state file)
are never deleted. A sweep runs right after the setting changes and then
every hour.

The choice is saved in LOG_DIR/log-retention.json so it survives restarts.
Before the first change, LOG_RETENTION_DAYS (default 0 = off) decides.
"""

from __future__ import annotations

import json
import os
import threading
import time

MIN_DAYS = 1
MAX_DAYS = 3650
DEFAULT_DAYS = 30     # shown in the page while the setting is off
SWEEP_SECONDS = 3600


class LogRetention:
    def __init__(self, log_dir: str, environ=None, clock=time.time) -> None:
        env = os.environ if environ is None else environ
        self.log_dir = log_dir
        self.state_path = os.path.join(log_dir, "log-retention.json")
        self.clock = clock
        try:
            initial = int(float(env.get("LOG_RETENTION_DAYS") or 0))
        except ValueError:
            print("LOG_RETENTION_DAYS is not a number, auto delete stays off", flush=True)
            initial = 0
        self.enabled, self.days = self._load(initial)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self.last_sweep = None
        self.last_deleted: list[str] = []
        self.deleted_total = 0
        self.last_error = None

    # -- state --------------------------------------------------------------

    def _load(self, initial: int) -> tuple[bool, int]:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            return bool(st.get("enabled")), _clamp(int(st.get("days") or DEFAULT_DAYS))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if initial > 0:
            return True, _clamp(initial)
        return False, DEFAULT_DAYS

    def _save(self) -> None:
        os.makedirs(self.log_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"enabled": self.enabled, "days": self.days, "changed": time.time()}, fh)
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "days": self.days,
                    "min_days": MIN_DAYS, "max_days": MAX_DAYS,
                    "last_sweep": self.last_sweep, "last_deleted": list(self.last_deleted),
                    "deleted_total": self.deleted_total, "error": self.last_error,
                    "now": self.clock()}

    def configure(self, enabled: bool, days) -> dict:
        """Change the setting (from the web UI); persisted, sweeps at once."""
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days != int(days):
            raise ValueError("days must be a whole number")
        days = int(days)
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}")
        with self._lock:
            changed = (bool(enabled), days) != (self.enabled, self.days)
            self.enabled, self.days = bool(enabled), days
        if changed:
            self._save()
            print(f"log auto delete {'on, ' + str(days) + ' days' if enabled else 'off'} "
                  f"(changed in the web UI)", flush=True)
            self._wake.set()
        return self.status()

    # -- sweeping -----------------------------------------------------------

    def sweep(self) -> list[str]:
        """Delete the logs older than ``days`` if the setting is on."""
        with self._lock:
            enabled, days = self.enabled, self.days
        if not enabled:
            return []
        cutoff = self.clock() - days * 86400
        deleted, errors = [], []
        try:
            names = sorted(os.listdir(self.log_dir))
        except FileNotFoundError:
            names = []
        for name in names:
            if not name.endswith(".jsonl") or name.startswith("."):
                continue
            path = os.path.join(self.log_dir, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                    continue
                os.remove(path)
                deleted.append(name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{name}: {exc.strerror}")
        with self._lock:
            self.last_sweep = self.clock()
            self.last_deleted = deleted
            self.deleted_total += len(deleted)
            self.last_error = "; ".join(errors) or None
        if deleted:
            print(f"log auto delete: removed {', '.join(deleted)} (older than {days} days)", flush=True)
        if errors:
            print(f"log auto delete: {self.last_error}", flush=True)
        return deleted

    def start(self) -> None:
        if self._thread is not None:
            return

        def run():
            while True:
                try:
                    self.sweep()
                except Exception as exc:  # keep the thread alive
                    print(f"log auto delete failed: {type(exc).__name__}: {exc}", flush=True)
                self._wake.wait(SWEEP_SECONDS)
                self._wake.clear()

        self._thread = threading.Thread(target=run, name="log-retention", daemon=True)
        self._thread.start()


def _clamp(days: int) -> int:
    return min(max(days, MIN_DAYS), MAX_DAYS)

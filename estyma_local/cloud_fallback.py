"""Answer the controller ourselves when the cloud is too slow or fails.

In ``proxy`` and ``hybrid`` the controller's report goes to the cloud and the
cloud's reply goes back. The controller only waits about 14 s for that reply
(measured from the proxy side: replies after 13.7 s still arrive, after
13.9 s the controller has already closed the connection and only polls again
about 95 s after its request). The cloud often takes longer than that.

With this setting on (``seconds`` > 0), a report the cloud has not answered
within ``seconds`` gets a normal-looking local reply instead: the usual
``ODCZYT`` read list and ``SERVER_INFO=REGISTER,1;DELAY,10``, built like in
``local`` mode. The same happens when the cloud answers with a 5xx status
(e.g. ``500`` or ``503 Service Temporarily Unavailable``) or cannot be
reached at all. The cloud request is not cut off: the cloud still gets the
report, and its late reply is logged as its own record (``late_for``).

The choice is saved in LOG_DIR/cloud-fallback.json so it survives restarts.
Before the first change, CLOUD_FALLBACK_SECONDS (default 12) decides;
``0`` turns it off.
"""

from __future__ import annotations

import json
import os
import threading
import time

MIN_SECONDS = 1
MAX_SECONDS = 60
DEFAULT_SECONDS = 12     # fires about 1.7 s before the controller gives up
DEVICE_GIVES_UP = 13.8   # seconds, measured on the proxy side (see the docstring)
REASONS = ("slow", "cloud_error", "unreachable")


class CloudFallback:
    def __init__(self, log_dir: str, environ=None, clock=time.time) -> None:
        env = os.environ if environ is None else environ
        self.log_dir = log_dir
        self.state_path = os.path.join(log_dir, "cloud-fallback.json")
        self.clock = clock
        raw = env.get("CLOUD_FALLBACK_SECONDS")
        try:
            initial = DEFAULT_SECONDS if raw in (None, "") else int(float(raw))
        except ValueError:
            print(f"CLOUD_FALLBACK_SECONDS is not a number, using {DEFAULT_SECONDS}", flush=True)
            initial = DEFAULT_SECONDS
        self.seconds = self._load(initial)
        self._lock = threading.Lock()
        self.counts = {r: 0 for r in REASONS}
        self.last: dict | None = None   # {"time", "reason", "detail"}
        self.late_replies = 0           # cloud replies that came after a local answer

    # -- state --------------------------------------------------------------

    def _load(self, initial: int) -> int:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return _check(json.load(fh).get("seconds"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            return _check(initial)
        except ValueError:
            print(f"CLOUD_FALLBACK_SECONDS must be 0 or {MIN_SECONDS}..{MAX_SECONDS}, "
                  f"using {DEFAULT_SECONDS}", flush=True)
            return DEFAULT_SECONDS

    def _save(self) -> None:
        os.makedirs(self.log_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seconds": self.seconds, "changed": time.time()}, fh)
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self.seconds > 0, "seconds": self.seconds,
                    "default_seconds": DEFAULT_SECONDS, "device_gives_up": DEVICE_GIVES_UP,
                    "min_seconds": MIN_SECONDS, "max_seconds": MAX_SECONDS,
                    "counts": dict(self.counts), "total": sum(self.counts.values()),
                    "late_replies": self.late_replies,
                    "last": dict(self.last) if self.last else None, "now": self.clock()}

    def configure(self, seconds) -> dict:
        """Change the setting (from the web UI); persisted."""
        seconds = _check(seconds)
        with self._lock:
            changed = seconds != self.seconds
            self.seconds = seconds
        if changed:
            self._save()
            print(f"cloud timeout fallback {'after ' + str(seconds) + ' s' if seconds else 'off'} "
                  f"(changed in the web UI)", flush=True)
        return self.status()

    # -- events (from the proxy) --------------------------------------------

    def answered_locally(self, reason: str, detail: str = "") -> None:
        with self._lock:
            self.counts[reason] = self.counts.get(reason, 0) + 1
            self.last = {"time": self.clock(), "reason": reason, "detail": detail}
        print(f"cloud fallback: answered the controller locally ({reason}"
              f"{': ' + detail if detail else ''})", flush=True)

    def late_reply(self) -> None:
        with self._lock:
            self.late_replies += 1


def _check(seconds) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds != int(seconds):
        raise ValueError("seconds must be a whole number")
    seconds = int(seconds)
    if seconds != 0 and not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be 0 (off) or between {MIN_SECONDS} and {MAX_SECONDS}")
    return seconds

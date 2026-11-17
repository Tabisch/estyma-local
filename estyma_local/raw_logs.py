"""Raw traffic logs: the debugging switch, and how they are kept small.

Every exchange between the controller and the cloud can be written to
``exchanges-YYYY-MM-DD.jsonl`` (one JSON object per line, UTC dates). That is
only needed to debug or to reverse-engineer the protocol; the history the
pages need is kept in ``history.sqlite3`` either way (see history.py). So the
raw logs are off by default and switched on the app settings page.

While they are on, the plain ``.jsonl`` file holds the current hour only.
At every hour change (and at start) it is gzipped and appended to that day's
``exchanges-YYYY-MM-DD.jsonl.gz`` as one more gzip member, which any gzip
tool reads as one file. Each record keeps the controller's report once (as
``text``; the parsed ``form`` copy older versions added is dropped), so a
day takes about 0.6 MB instead of 150 MB.

The choice is saved in LOG_DIR/raw-logs.json so it survives restarts.
Before the first change, RAW_LOGS (default 0 = off) decides.
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import threading
import time
import zlib

STATE_FILE = "raw-logs.json"


class RawLogSetting:
    def __init__(self, log_dir: str, environ=None, clock=time.time) -> None:
        env = os.environ if environ is None else environ
        self.log_dir = log_dir
        self.state_path = os.path.join(log_dir, STATE_FILE)
        self.clock = clock
        initial = (env.get("RAW_LOGS") or "").strip().lower() in ("1", "true", "yes", "on")
        self.enabled = self._load(initial)
        self._lock = threading.Lock()
        self.listeners = []  # called with the new value, e.g. ExchangeLog.flush

    def _load(self, initial: bool) -> bool:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return bool(json.load(fh)["enabled"])
        except (OSError, ValueError, TypeError, KeyError):
            return initial

    def _save(self) -> None:
        os.makedirs(self.log_dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"enabled": self.enabled, "changed": time.time()}, fh)
        os.replace(tmp, self.state_path)

    def status(self) -> dict:
        files = sorted(glob.glob(os.path.join(self.log_dir, "exchanges-*.jsonl*")))
        size = 0
        for f in files:
            try:
                size += os.path.getsize(f)
            except OSError:
                pass
        return {"enabled": self.enabled, "files": len(files), "size": size, "now": self.clock()}

    def configure(self, enabled) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        with self._lock:
            changed = enabled != self.enabled
            self.enabled = enabled
        if changed:
            self._save()
            print(f"raw traffic logs {'on' if enabled else 'off'} (changed in the web UI)", flush=True)
            for fn in self.listeners:
                try:
                    fn(enabled)
                except Exception as exc:
                    print(f"raw logs listener: {type(exc).__name__}: {exc}", flush=True)
        return self.status()


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def slim_line(line: str) -> str:
    """Drop the parsed ``form`` copy of a request body that also has ``text``."""
    if '"form"' not in line:
        return line
    try:
        rec = json.loads(line)
    except ValueError:
        return line
    body = (rec.get("request") or {}).get("body") or {}
    if "text" in body and "form" in body:
        del body["form"]
        return json.dumps(rec, ensure_ascii=False)
    return line


def pack(plain: str, truncate: bool = False) -> str | None:
    """Gzip ``exchanges-D.jsonl`` onto ``exchanges-D.jsonl.gz`` (one more
    member), slimming each line. The plain file is removed, or emptied with
    ``truncate`` (the file still being written, called with its lock held).
    Returns the .gz path, or None if there was nothing to pack."""
    try:
        if os.path.getsize(plain) == 0:
            if not truncate:
                os.remove(plain)
            return None
    except FileNotFoundError:
        return None
    gz = plain + ".gz"
    mtime = os.path.getmtime(plain)
    with open(plain, encoding="utf-8", errors="replace") as src, open(gz, "ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6,
                           filename=os.path.basename(plain)) as dst:
            for line in src:
                line = line.rstrip("\n")
                if line:
                    dst.write((slim_line(line) + "\n").encode("utf-8"))
        raw.flush()
        os.fsync(raw.fileno())
    if truncate:
        open(plain, "w").close()
    else:
        os.remove(plain)
        # Keep the day's age, so auto delete (retention.py) still sees it.
        os.utime(gz, (mtime, mtime))
    return gz


def read_lines(path: str):
    """Lines of a .jsonl or .jsonl.gz log. A .gz cut short (e.g. by a power
    cut while packing) yields what is readable."""
    if not path.endswith(".gz"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            yield from fh
        return
    d = zlib.decompressobj(wbits=31)
    rest = b""
    with open(path, "rb") as fh:
        broken = False
        while not broken:
            buf = fh.read(1 << 16)
            if not buf:
                break
            while buf:
                try:
                    data = d.decompress(buf)
                except zlib.error:
                    broken = True
                    break
                if d.eof:  # next gzip member
                    buf = d.unused_data
                    d = zlib.decompressobj(wbits=31)
                else:
                    buf = b""
                rest += data
                *lines, rest = rest.split(b"\n")
                for ln in lines:
                    yield ln.decode("utf-8", errors="replace") + "\n"
    if rest.strip():
        yield rest.decode("utf-8", errors="replace")


def read_records(path: str):
    for line in read_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def exchange_files(log_dir: str) -> list[str]:
    """All exchange logs, oldest day first; a day's .gz before its .jsonl."""
    files = glob.glob(os.path.join(log_dir, "exchanges-*.jsonl")) + \
        glob.glob(os.path.join(log_dir, "exchanges-*.jsonl.gz"))
    return sorted(files, key=lambda p: (os.path.basename(p).split(".")[0], not p.endswith(".gz")))

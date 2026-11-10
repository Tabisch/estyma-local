"""Docker HEALTHCHECK: is the server answering?

    python3 -m estyma_local.healthcheck

Healthy (exit 0) when the controller port (LISTEN_PORT) accepts connections
and, unless the web UI is off (UI_PORT=0), ``/api/health`` on the web UI
answers. It does not look at whether the controller reports: a controller
that is switched off does not make the server unhealthy. The age of the last
report is in the ``/api/health`` answer.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.request

from .app import env

TIMEOUT = 5


def check(environ=None) -> str | None:
    """None if healthy, else what is wrong."""
    port = int(env("LISTEN_PORT", environ=environ))
    try:
        socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT).close()
    except OSError as exc:
        return f"controller port {port}: {exc}"
    ui = int(env("UI_PORT", environ=environ))
    if ui:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{ui}/api/health", timeout=TIMEOUT) as r:
                if not json.load(r).get("ok"):
                    return "web UI: not ok"
        except (OSError, ValueError) as exc:
            return f"web UI port {ui}: {exc}"
    return None


def main() -> int:
    problem = check()
    if problem:
        print(problem)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

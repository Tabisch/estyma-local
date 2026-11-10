"""Every environment variable the image understands, in one place.

``gendocs.py`` builds the tables in ``docs/configuration.md`` and the
``.env.example`` file from this list, and ``tests/test_docs.py`` checks that

- every variable the code reads is listed here (and nothing else),
- ``docker-compose.yml`` passes on every variable marked ``compose`` with the
  same default.

To add a variable: read it in the code, add an ``Env`` here, add it to
``docker-compose.yml`` if it is marked ``compose``, run
``python3 -m estyma_local.gendocs``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sections of docs/configuration.md, in order: key -> (title, intro).
SECTIONS = {
    "proxy": ("Proxy", "The `estyma-proxy` service, default command of the image."),
    "local": ("Local server", "Same service, see [operating-modes.md](operating-modes.md)."),
    "cloud": ("Cloud monitoring", "Same service, see [cloud-monitoring.md](cloud-monitoring.md)."),
    "mqtt": ("MQTT / Home Assistant", "Same service, see [home-assistant.md](home-assistant.md)."),
    "recorder": ("Standalone cloud recorder", "`python3 -m estyma_local.cloud_recorder` reads the same "
                 "`ESTYMA_*` variables as cloud monitoring, plus these."),
    "host": ("Compose / `.env` (host side)", "Only used by `docker-compose.yml`, not inside the container."),
}


@dataclass(frozen=True)
class Env:
    name: str
    default: str          # as documented; "" = empty / unset
    section: str
    help: str
    compose: bool = True  # docker-compose.yml passes it on as ${NAME:-default}
    example: bool = True  # listed in .env.example
    secret: bool = False  # .env.example leaves it empty


ENV = [
    # -- proxy --------------------------------------------------------------
    Env("UPSTREAM", "", "proxy", "Provider server the requests are forwarded to, e.g. "
        "`http://203.0.113.10:80`. Required unless `MODE=local` without `CLOUD_MIRROR` / `LOCAL_FALLBACK` (set directly in `docker-compose.yml`)",
        compose=False, example=False),
    Env("UPSTREAM_TIMEOUT", "30", "proxy", "Seconds to wait for the provider before answering `502` "
        "(set directly in `docker-compose.yml`)", compose=False, example=False),
    Env("LISTEN_HOST", "0.0.0.0", "proxy", "Bind address inside the container", compose=False, example=False),
    Env("LISTEN_PORT", "80", "proxy", "Proxy port inside the container (the host port is `PROXY_PORT`)",
        compose=False, example=False),
    Env("LOG_DIR", "/logs", "proxy", "Where the logs and state files are written (the `estyma-logs` volume)",
        compose=False, example=False),
    Env("MAX_BODY_LOG", "0", "proxy", "Max bytes of each body kept in the log, `0` = unlimited",
        compose=False, example=False),
    Env("UI_PORT", "8089", "proxy", "Web UI port. Inside the container `0` turns the UI off; in "
        "`docker-compose.yml` it is the host port (inside it stays 8089)"),
    Env("UI_USER", "", "proxy", "Basic auth user for the web UI. Also needed for writes, switching "
        "cloud monitoring and deleting logs"),
    Env("UI_PASSWORD", "", "proxy", "Basic auth password; auth is on only when user and password are "
        "both set", secret=True),
    Env("MONITOR_HISTORY_HOURS", "48", "proxy", "Hours of decoded values the monitor keeps in memory "
        "for its charts (reloaded from the logs on start)", compose=False, example=False),
    # -- local server -------------------------------------------------------
    Env("MODE", "proxy", "local", "`proxy`, `hybrid` or `local`"),
    Env("WRITES_ENABLED", "0", "local", "`1` = allow local writes (boiler target)"),
    Env("ALLOW_POWER_WRITES", "0", "local", "`1` = also allow switching the boiler on/off; needs "
        "`WRITES_ENABLED=1`"),
    Env("ALLOW_UNTESTED_WRITES", "0", "local", "`1` = also allow the settings whose write path is "
        "untested (all but boiler target and on/off); needs `WRITES_ENABLED=1`"),
    Env("SETTINGS_READ_MINUTES", "60", "local", "Minutes between reads of all settings in "
        "`hybrid`/`local`, `0` = only on start and on request"),
    Env("WRITE_TIMEOUT", "120", "local", "Seconds until a write without read-back is marked failed",
        compose=False, example=False),
    Env("WRITE_MIN_INTERVAL", "10", "local", "Minimum seconds between two requests for the same register",
        compose=False, example=False),
    Env("LOCAL_FALLBACK", "0", "local", "`1` = in `proxy`/`hybrid`, answer locally when the cloud "
        "fails instead of `502`"),
    Env("CLOUD_MIRROR", "0", "local", "`1` = in `local`, still forward each report to the cloud and "
        "ignore its reply"),
    Env("CONTROLLER_TZ", "Europe/Warsaw", "local", "Time zone of the controller clock, for the clock "
        "sync in `local` mode"),
    Env("CLOCK_SYNC_HOURS", "6", "local", "Hours between clock syncs in `local` mode, `0` = off",
        compose=False, example=False),
    Env("POLL_DELAY", "10", "local", "`DELAY` value in local replies (the controller's pause between polls)",
        compose=False, example=False),
    Env("STATE_FILE", "", "local", "Last reported registers and the learned register "
        "list, kept across restarts; empty = `state.json` in `LOG_DIR`", compose=False, example=False),
    # -- cloud monitoring ---------------------------------------------------
    Env("ESTYMA_EMAIL", "", "cloud", "Login e-mail of your Estyma cloud account; without it cloud "
        "monitoring is unavailable"),
    Env("ESTYMA_PASSWORD", "", "cloud", "Password of that account", secret=True),
    Env("ESTYMA_PASSWORD_B64", "", "cloud", "Base64 of the password, overrides `ESTYMA_PASSWORD`. Use it "
        "when the password starts or ends with a space, because `.env` files and some UIs trim those "
        "(`printf ' my password' \\| base64`)", secret=True),
    Env("ESTYMA_PASSWORD_FILE", "", "cloud", "Path to a file holding the password (e.g. a Docker "
        "secret); only a trailing newline is removed", compose=False, example=False),
    Env("ESTYMA_DOMAIN", "igneo.pl", "cloud", "Which Estyma cloud to log in to", compose=False, example=False),
    Env("ESTYMA_LANGUAGE", "english", "cloud", "Language for the wrapper's value names",
        compose=False, example=False),
    Env("CLOUD_MONITOR", "0", "cloud", "Initial on/off state, only used until it is switched in the web UI"),
    Env("CLOUD_INTERVAL", "30", "cloud", "Seconds between cloud polls"),
    # -- MQTT ---------------------------------------------------------------
    Env("MQTT_HOST", "", "mqtt", "Broker host name or IP; empty = publisher off"),
    Env("MQTT_PORT", "1883", "mqtt", "Broker port. Without compose it defaults to `8883` with `MQTT_TLS=1`; "
        "compose always passes `1883`, so set `8883` there for TLS"),
    Env("MQTT_USER", "", "mqtt", "Broker login, if needed"),
    Env("MQTT_PASSWORD", "", "mqtt", "Broker password, if needed", secret=True),
    Env("MQTT_TLS", "0", "mqtt", "`1` = connect with TLS (system CA certificates)"),
    Env("MQTT_PREFIX", "estyma", "mqtt", "First part of the topics"),
    Env("MQTT_DEVICE_ID", "boiler", "mqtt", "Second part of the topics and of the unique IDs; change "
        "only for a second boiler"),
    Env("HA_DISCOVERY_PREFIX", "homeassistant", "mqtt", "Home Assistant's discovery prefix"),
    Env("MQTT_STALE_SECONDS", "300", "mqtt", "Seconds without a controller report until the device is "
        "unavailable. Keep it well above 100 s: after a slow or failed cloud reply the controller "
        "waits about 100 s before its next report"),
    Env("FUEL_KWH_PER_KG", "", "mqtt", "Calorific value for the energy sensor; empty = controller "
        "setting, else 4.8"),
    # -- standalone recorder ------------------------------------------------
    Env("INTERVAL", "30", "recorder", "Seconds between polls", compose=False, example=False),
    Env("IDLE_IF_UNCONFIGURED", "", "recorder", "`1` = idle instead of exiting without credentials",
        compose=False, example=False),
    # -- host side ----------------------------------------------------------
    Env("PROXY_PORT", "8088", "host", "Host port of the proxy; the router's DNAT rule points here"),
]

BY_NAME = {e.name: e for e in ENV}
assert len(BY_NAME) == len(ENV), "variable listed twice"
assert all(e.section in SECTIONS for e in ENV)

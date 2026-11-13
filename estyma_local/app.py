"""Start everything: read the configuration, build the parts, wire them up.

    python3 -m estyma_local            (the image's default command)

``load_config`` turns environment variables (defaults from ``config.py``)
and command line flags into one ``Settings`` object; ``run`` builds the
controller, the proxy server, the web UI, the monitor, cloud monitoring and
the MQTT publisher from it and serves until stopped.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import timedelta, timezone

from . import config as K
from .controller import Controller
from .server import ExchangeLog, make_server


def env(name: str, default: str | None = None, environ=None) -> str | None:
    """Environment variable, else ``default``, else the default in config.py."""
    environ = os.environ if environ is None else environ
    if name in environ:
        return environ[name]
    if default is not None:
        return default
    d = K.BY_NAME[name].default
    return d if d != "" else None


def env_flag(name: str, environ=None) -> bool:
    return (env(name, environ=environ) or "").strip().lower() in ("1", "true", "yes", "on")


def load_tz(name: str):
    """Time zone of the controller clock; fixed UTC+1 if tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:
        print(f"time zone {name!r} not available ({exc}), using UTC+1", flush=True)
        return timezone(timedelta(hours=1))


@dataclass
class Settings:
    listen_host: str
    listen_port: int
    upstream: str | None
    upstream_timeout: float
    log_dir: str
    max_body_log: int
    ui_port: int
    ui_user: str | None
    ui_password: str | None
    monitor_history_hours: float
    mode: str
    state_file: str | None
    controller_tz: str
    clock_sync_hours: float
    poll_delay: int
    write_timeout: float
    settings_read_minutes: float
    write_min_interval: float
    writes_enabled: bool
    allow_power_writes: bool
    allow_untested_writes: bool
    local_fallback: bool
    cloud_mirror: bool
    mqtt_host: str | None


def load_config(argv=None, environ=None) -> Settings:
    """Command line flags win over environment variables, which win over the
    defaults in config.py."""
    e = lambda name, default=None: env(name, default, environ)  # noqa: E731
    p = argparse.ArgumentParser(prog="python3 -m estyma_local",
                                description="Local server for the Estyma heating controller.")
    p.add_argument("--listen-host", default=e("LISTEN_HOST"))
    p.add_argument("--listen-port", type=int, default=int(e("LISTEN_PORT")))
    p.add_argument("--upstream", default=e("UPSTREAM"),
                   help="e.g. http://203.0.113.10:80 (provider's real address)")
    p.add_argument("--upstream-timeout", type=float, default=float(e("UPSTREAM_TIMEOUT")))
    # Outside the image (which sets LOG_DIR=/logs) the logs go to ./logs.
    p.add_argument("--log-dir", default=e("LOG_DIR", "logs"))
    p.add_argument("--max-body-log", type=int, default=int(e("MAX_BODY_LOG")))
    p.add_argument("--ui-port", type=int, default=int(e("UI_PORT")),
                   help="port of the web UI, 0 = disabled")
    p.add_argument("--ui-user", default=e("UI_USER"))
    p.add_argument("--ui-password", default=e("UI_PASSWORD"))
    p.add_argument("--monitor-history-hours", type=float, default=float(e("MONITOR_HISTORY_HOURS")))
    p.add_argument("--mode", default=e("MODE"), choices=["proxy", "hybrid", "local"])
    p.add_argument("--state-file", default=e("STATE_FILE"))  # None: state.json in LOG_DIR
    p.add_argument("--controller-tz", default=e("CONTROLLER_TZ"))
    p.add_argument("--clock-sync-hours", type=float, default=float(e("CLOCK_SYNC_HOURS")))
    p.add_argument("--poll-delay", type=int, default=int(e("POLL_DELAY")))
    p.add_argument("--write-timeout", type=float, default=float(e("WRITE_TIMEOUT")))
    p.add_argument("--settings-read-minutes", type=float, default=float(e("SETTINGS_READ_MINUTES")))
    p.add_argument("--write-min-interval", type=float, default=float(e("WRITE_MIN_INTERVAL")))
    args = p.parse_args(argv)
    flag = lambda name: env_flag(name, environ)  # noqa: E731
    cfg = Settings(**vars(args), writes_enabled=flag("WRITES_ENABLED"),
                   allow_power_writes=flag("ALLOW_POWER_WRITES"),
                   allow_untested_writes=flag("ALLOW_UNTESTED_WRITES"),
                   local_fallback=flag("LOCAL_FALLBACK"), cloud_mirror=flag("CLOUD_MIRROR"),
                   mqtt_host=e("MQTT_HOST"))
    if not cfg.upstream and (cfg.mode != "local" or cfg.cloud_mirror or cfg.local_fallback):
        p.error("UPSTREAM is required (env var or --upstream) unless MODE=local")
    return cfg


def _describe(cfg: Settings) -> None:
    where = {"proxy": f"forwarding to {cfg.upstream}",
             "hybrid": f"forwarding to {cfg.upstream} (hybrid: local writes added)",
             "local": "answering locally" + (f", mirroring to {cfg.upstream}" if cfg.cloud_mirror else "")}
    print(f"estyma-proxy listening on {cfg.listen_host}:{cfg.listen_port}, mode {cfg.mode}, "
          f"{where[cfg.mode]}, logging to {os.path.abspath(cfg.log_dir)}", flush=True)
    w = cfg.writes_enabled
    print(f"writes {'ENABLED' if w else 'off'}"
          f"{', on/off allowed' if w and cfg.allow_power_writes else ''}"
          f"{', untested settings allowed' if w and cfg.allow_untested_writes else ''}"
          f"{', local fallback on' if cfg.local_fallback else ''}", flush=True)


def _start_web(cfg: Settings, log: ExchangeLog, ctl: Controller) -> None:
    """Monitor, optional cloud monitoring and the web UI on UI_PORT."""
    from .monitor import Monitor
    from .web import start_web
    mon = Monitor(cfg.log_dir, cfg.monitor_history_hours)
    log.listeners.append(mon.ingest)
    mon.start()
    cloud = None
    try:
        from .cloud_monitor import CloudMonitor
        cloud = CloudMonitor(cfg.log_dir)
        cloud.start()
        print(f"cloud monitor: {'configured' if cloud.configured else cloud.problem}, "
              f"{'on' if cloud.enabled and cloud.configured else 'off'} "
              f"(switch it in the web UI)", flush=True)
    except Exception as exc:  # never keep the proxy from starting
        print(f"cloud monitor unavailable: {type(exc).__name__}: {exc}", flush=True)
    retention = None
    try:
        from .retention import LogRetention
        retention = LogRetention(cfg.log_dir)
        retention.start()
        print(f"log auto delete: {'after ' + str(retention.days) + ' days' if retention.enabled else 'off'} "
              f"(change it in the web UI)", flush=True)
    except Exception as exc:  # never keep the proxy from starting
        print(f"log auto delete unavailable: {type(exc).__name__}: {exc}", flush=True)
    start_web(cfg.listen_host, cfg.ui_port, cfg.log_dir, cfg.ui_user, cfg.ui_password,
              monitor=mon, controller=ctl, cloud=cloud, retention=retention)
    auth = "basic auth on" if cfg.ui_user and cfg.ui_password else "no auth"
    print(f"web UI on http://{cfg.listen_host}:{cfg.ui_port}/ ({auth})", flush=True)


def _start_mqtt(ctl: Controller):
    try:
        from .mqtt_publisher import from_env
        return from_env(ctl)
    except Exception as exc:  # never keep the proxy from starting
        print(f"mqtt publisher unavailable: {type(exc).__name__}: {exc}", flush=True)
        return None


def run(cfg: Settings) -> int:
    log = ExchangeLog(cfg.log_dir, cfg.max_body_log)
    ctl = Controller(cfg.mode, cfg.log_dir, writes_enabled=cfg.writes_enabled,
                     allow_power_writes=cfg.allow_power_writes,
                     allow_untested_writes=cfg.allow_untested_writes,
                     settings_read_interval=cfg.settings_read_minutes * 60,
                     write_timeout=cfg.write_timeout, write_min_interval=cfg.write_min_interval,
                     clock_sync_interval=cfg.clock_sync_hours * 3600,
                     tz=load_tz(cfg.controller_tz), state_file=cfg.state_file)
    server = make_server(cfg.listen_host, cfg.listen_port, cfg.upstream, cfg.upstream_timeout,
                         log, ctl, local_fallback=cfg.local_fallback,
                         cloud_mirror=cfg.cloud_mirror, poll_delay=cfg.poll_delay)
    _describe(cfg)
    if cfg.ui_port:
        _start_web(cfg, log, ctl)
    mqtt = _start_mqtt(ctl) if cfg.mqtt_host else None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if mqtt is not None:
            mqtt.stop()
        server.server_close()
        ctl.save_state(force=True)
    return 0


def main(argv=None) -> int:
    return run(load_config(argv))


if __name__ == "__main__":
    sys.exit(main())

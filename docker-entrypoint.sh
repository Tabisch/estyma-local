#!/bin/sh
# Start the server as the unprivileged user "estyma" (uid 1000).
#
# Started as root (the default): make sure the log volume belongs to estyma
# (volumes created by older images belong to root), then drop to estyma. It
# keeps only the right to bind ports below 1024, because the controller's
# traffic arrives on port 80 inside the container.
# Started with --user / user: already unprivileged, run as is.
set -e

if [ "$(id -u)" = "0" ]; then
    LOG_DIR="${LOG_DIR:-/logs}"
    mkdir -p "$LOG_DIR"
    if [ -n "$(find "$LOG_DIR" \! -user estyma -print -quit 2>/dev/null)" ]; then
        chown -R estyma:estyma "$LOG_DIR"
    fi
    exec setpriv --reuid=estyma --regid=estyma --init-groups \
        --inh-caps=-all,+net_bind_service --ambient-caps=+net_bind_service \
        --bounding-set=-all,+net_bind_service --no-new-privs \
        "$@"
fi
exec "$@"

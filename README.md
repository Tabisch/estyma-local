# estyma-local

Goal: replace the Estyma heating system's cloud web UI with a locally hosted
one, because the controller talks to the provider's server over unencrypted
HTTP.

Step 1 (this repo, for now): a **transparent logging proxy** that sits between
the heating controller and the provider's server. It forwards every request
unchanged and records each request/response pair, so the protocol can be
reverse engineered and a local replacement built later.

## How it works

```
heating controller --HTTP--> estyma-proxy --HTTP--> provider server
                                  |
                                  +--> logs/exchanges-YYYY-MM-DD.jsonl
```

- Python 3 standard library only, no dependencies.
- Request headers are forwarded verbatim (order and casing kept, including
  `Host`), bodies byte for byte. Only hop-by-hop headers (`Connection`,
  `Transfer-Encoding`, ...) are normalised; chunked bodies are re-sent with a
  `Content-Length`.
- If the provider is unreachable the controller gets a `502` and the attempt
  is still logged.

## Getting the controller's traffic to the proxy

You need to know the provider's hostname and real IP first. Look at the
controller's settings or your router's DNS log, then `nslookup <hostname>`.

The proxy listens on host port **8088** by default (`PROXY_PORT` in
`docker-compose.yml`), because port 80 is usually taken. The controller always
connects to port 80, so the traffic has to be redirected *and* re-ported:

1. **DNAT rule on the router (recommended).** Rewrite
   `controller -> provider_ip:80` to `proxy_host:8088`. `UPSTREAM` is the
   provider's IP. The rule must only match traffic coming from the controller,
   not from the proxy, or the proxy's own upstream requests would loop.
   Example for an iptables/OpenWrt router:

   ```sh
   iptables -t nat -A PREROUTING -s <controller_ip> -d <provider_ip> -p tcp --dport 80 \
     -j DNAT --to-destination <proxy_host>:8088
   # needed when controller and proxy are in the same subnet, so replies go back via the router
   iptables -t nat -A POSTROUTING -s <controller_ip> -d <proxy_host> -p tcp --dport 8088 -j MASQUERADE
   ```

   On OpenWrt the same thing is a port forward / DNAT rule in
   *Network → Firewall → Port Forwards* (source IP = controller, external IP =
   provider IP, external port 80, internal IP = proxy host, internal port 8088).
2. **DNS override.** Pointing the provider's hostname at the proxy host only
   works if something on that host answers on port 80 and forwards to 8088
   (e.g. an existing reverse proxy with a rule for that hostname), since the
   controller cannot be told to use another port. It also requires the
   controller to use your DNS server instead of a hard-coded IP.

## Running

With Docker, using the prebuilt image (amd64, arm64, armv7), which GitHub
Actions publishes to `ghcr.io/tabisch/estyma-local:latest` on every push to
`main`:

```sh
docker volume create estyma-logs
docker run -d --name estyma-proxy -p 8088:80 -p 8089:8089 \
  -e UPSTREAM=http://203.0.113.10:80 -v estyma-logs:/logs \
  ghcr.io/tabisch/estyma-local:latest
```

Or with compose:

```sh
# edit UPSTREAM in docker-compose.yml first (host port: PROXY_PORT, default 8088)
docker compose up -d
docker compose logs -f        # one line per exchange
```

Logs live in the named Docker volume `estyma-logs` (shared with the
optional cloud recorder), not in a folder next to the compose file.

### Downloading the logs

The proxy also serves a small web UI on port **8089** (`UI_PORT`):
`http://<proxy-host>:8089/` lists every log file with size and date, with a
download link per file and a "Download all (zip)" button. It is read-only.
The logs contain whatever the controller sends (device IDs, PINs, ...), so
keep the port on your LAN, and set `UI_USER` / `UI_PASSWORD` (in `.env`) to
turn on basic auth. `UI_PORT=0` disables it.

Without Docker:

```sh
UPSTREAM=http://203.0.113.10:80 LISTEN_PORT=8088 python3 proxy/estyma_proxy.py
# or: python3 proxy/estyma_proxy.py --upstream http://203.0.113.10 --listen-port 8080
```

| Variable           | Default    | Meaning                                         |
|--------------------|------------|-------------------------------------------------|
| `UPSTREAM`         | (required) | Provider server, e.g. `http://203.0.113.10:80`  |
| `LISTEN_HOST`      | `0.0.0.0`  | Bind address                                    |
| `LISTEN_PORT`      | `80`       | Bind port                                       |
| `UPSTREAM_TIMEOUT` | `30`       | Seconds to wait for the provider                |
| `LOG_DIR`          | `logs`     | Where the JSONL files go                        |
| `MAX_BODY_LOG`     | `0`        | Max bytes per logged body, `0` = unlimited      |
| `UI_PORT`          | `8089`     | Log download web UI, `0` = off                  |
| `UI_USER` / `UI_PASSWORD` | (unset) | Basic auth for the log UI when both are set |

## Building the image on your own GitHub Actions runner

`.github/workflows/docker.yml` builds the image and pushes it to
`ghcr.io/tabisch/estyma-local` on every push to `main`. By default it runs on
GitHub's hosted runners. To run it on your own machine instead:

1. On GitHub: *Settings → Actions → Runners → New self-hosted runner*, pick
   Linux and your CPU, and run the commands it shows on the machine (any
   Linux box with Docker). Install it as a service so it survives reboots:
   `sudo ./svc.sh install && sudo ./svc.sh start`.
2. The runner's user must be able to use Docker: `sudo usermod -aG docker <runner-user>`,
   then restart the service.
3. *Settings → Secrets and variables → Actions → Variables*: add `RUNNER` with
   the value `self-hosted`. Delete the variable to go back to GitHub's runners.
4. Re-run the workflow (*Actions → Docker image → Run workflow*).

The multi-arch build (amd64, arm64, armv7) uses QEMU, which the workflow sets
up on the runner itself. On a Raspberry Pi this works but is slow.

## Log format

One JSON object per line in `logs/exchanges-YYYY-MM-DD.jsonl` (UTC dates):

```json
{
  "id": "b4237dcc89c1",
  "time": "2026-11-07T13:23:15.024+00:00",
  "client": "192.168.1.50:50012",
  "request": {
    "method": "GET", "target": "/api/status?id=42", "path": "/api/status",
    "query": [["id", "42"]], "http_version": "HTTP/1.1",
    "headers": [["Host", "provider.example"], ["User-Agent", "..."]],
    "body": {"length": 0}
  },
  "response": {
    "time": "...", "status": 200, "reason": "OK", "http_version": "HTTP/1.1",
    "headers": [["Content-Type", "text/plain"]],
    "body": {"length": 12, "text": "..."}
  },
  "duration_ms": 41.2
}
```

Bodies are stored as `text` when they are valid UTF-8, otherwise as `base64`.
Form-encoded and JSON bodies are also decoded into `form` / `json`. Failed
exchanges have an `error` field instead of `response`.

Quick overview of what was captured (endpoints, parameter names, status
codes):

```sh
python3 proxy/summarize_logs.py logs/
```

Logs contain whatever the controller sends, possibly including account
credentials or device IDs. Keep them private.

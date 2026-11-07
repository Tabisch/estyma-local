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

1. **DNS override (easiest).** In your router, Pi-hole, AdGuard Home, or
   dnsmasq, point the provider's hostname at the machine running the proxy.
   Set `UPSTREAM` to the provider's **IP address** (not the hostname, or the
   proxy would loop back to itself). This only works if the controller uses
   your DNS server and not a hard-coded one or a raw IP.
2. **NAT / port-forward rule.** If the controller connects to an IP directly,
   add a DNAT rule on the router (e.g. OpenWrt/iptables) that rewrites
   `controller -> provider_ip:80` to `proxy_ip:80`. `UPSTREAM` is then the
   provider IP as well; the rule must only match traffic from the controller,
   not from the proxy.

## Running

With Docker, using the prebuilt image (amd64, arm64, armv7), which GitHub
Actions publishes to `ghcr.io/tabisch/estyma-local:latest` on every push to
`main`:

```sh
docker run -d --name estyma-proxy -p 80:80 \
  -e UPSTREAM=http://203.0.113.10:80 -v "$PWD/logs:/logs" \
  ghcr.io/tabisch/estyma-local:latest
```

Or with compose:

```sh
# edit UPSTREAM in docker-compose.yml first
docker compose up -d
docker compose logs -f        # one line per exchange
```

Without Docker:

```sh
UPSTREAM=http://203.0.113.10:80 LISTEN_PORT=80 python3 proxy/estyma_proxy.py
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

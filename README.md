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

## Local build pipeline (own registry, no GitHub Actions)

Everything runs on your own machines: a private registry holds the image, and
`scripts/build-and-push.sh` pulls the code from git, builds the image and pushes it.

### 1. Run a registry (once)

On the machine that should host it (NAS, server, Pi):

```sh
docker compose -f deploy/registry/docker-compose.yml up -d
curl http://localhost:5000/v2/_catalog      # -> {"repositories":[]}
```

Images are stored in `deploy/registry/data/`. The registry speaks plain HTTP on
port 5000 and has no login, so keep it on your LAN only.

### 2. Allow plain HTTP on every Docker host that pushes or pulls

Docker refuses HTTP registries except `localhost`. On every other machine
(build machine, the box that runs the proxy), add the registry to
`/etc/docker/daemon.json` and restart Docker:

```json
{ "insecure-registries": ["registry-host.lan:5000"] }
```

```sh
sudo systemctl restart docker
```

If you would rather use HTTPS: put the registry behind a reverse proxy with a
certificate (e.g. Caddy or Traefik with your own domain), then skip this step
and run the script with `INSECURE=0`.

### 3. Build and push

```sh
# on the build machine; only needs git and docker
REGISTRY=registry-host.lan:5000 ./scripts/build-and-push.sh
```

This keeps a checkout in `~/.cache/estyma-local-src`, updates it to the latest
`main`, builds, and pushes `registry-host.lan:5000/estyma-local:latest` plus a
tag with the commit hash. You can also copy just the script to the build
machine; it clones the repo itself.

The default build is for the build machine's own CPU. If the proxy runs on a
different one (e.g. building on x86 for a Raspberry Pi), build multi-arch:

```sh
# once per build machine: register QEMU emulators
docker run --privileged --rm tonistiigi/binfmt --install all

REGISTRY=registry-host.lan:5000 PLATFORMS=linux/amd64,linux/arm64,linux/arm/v7 \
  ./scripts/build-and-push.sh
```

The script creates a buildx builder called `estyma-builder` configured for
the HTTP registry. If you change `REGISTRY` later, remove it first with
`docker buildx rm estyma-builder`.

### 4. Run the proxy from your registry

In `docker-compose.yml` set
`image: registry-host.lan:5000/estyma-local:latest`, then on updates:

```sh
docker compose pull && docker compose up -d
```

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

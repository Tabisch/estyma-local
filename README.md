# estyma-local

Goal: replace the Estyma heating system's cloud web UI with a locally hosted
one, because the controller talks to the provider's server over unencrypted
HTTP.

Step 1: a **transparent logging proxy** between the heating controller and the
provider's server. It forwards every request unchanged and records each
request/response pair, so the protocol can be reverse engineered and a local
replacement built later. An optional **cloud recorder** polls the provider's
web API at the same time, so cloud values can be matched to raw controller
data.

This README covers one setup: Docker (Portainer stack or docker compose) plus
a NAT rule on the firewall/router.

## How it works

```
                      firewall DNAT
heating controller --- provider_ip:80 ---> proxy_host:8088 (estyma-proxy) ---> provider server
                                                  |
cloud-recorder (optional) --- provider web API    +--> volume estyma-logs
        |                                                 exchanges-YYYY-MM-DD.jsonl
        +-----------------------------------------------> cloud-YYYY-MM-DD.jsonl
                                                  |
                                   browser ---> proxy_host:8089 (monitor + log download UI)
```

- Request headers are forwarded verbatim (order and casing kept, including
  `Host`), bodies byte for byte. Only hop-by-hop headers (`Connection`,
  `Transfer-Encoding`, ...) are normalised.
- If the provider is unreachable the controller gets a `502` and the attempt
  is still logged.

## 1. Firewall NAT rule

The controller always connects to `provider_ip:80`. A DNAT rule on the
firewall/router sends that traffic to the proxy instead:

| Match                                                        | Rewrite to             |
|--------------------------------------------------------------|------------------------|
| source `<controller_ip>`, destination `<provider_ip>`, TCP 80 | `<proxy_host>:8088`    |

Find `<provider_ip>` in the firewall's connection log for the controller.

- Match **only the controller's source IP**. The proxy itself must still reach
  the real provider, otherwise its upstream requests would loop back to it.
- If the controller and the proxy host are in the same subnet, also add a
  masquerade/SNAT rule for that traffic, so the proxy's replies go back
  through the firewall and the controller accepts them.

iptables / OpenWrt (fw3) example:

```sh
iptables -t nat -A PREROUTING  -s <controller_ip> -d <provider_ip> -p tcp --dport 80 \
  -j DNAT --to-destination <proxy_host>:8088
# only needed when controller and proxy host share a subnet
iptables -t nat -A POSTROUTING -s <controller_ip> -d <proxy_host> -p tcp --dport 8088 -j MASQUERADE
```

On OpenWrt's web UI this is *Network → Firewall → Port Forwards*: source IP =
controller, external IP = provider IP, external port 80, internal IP = proxy
host, internal port 8088. Other firewalls (OPNsense/pfSense, MikroTik, ...)
call it a destination NAT / port forward rule with the same fields.

Check it works: the proxy logs one line per request (`docker logs`), and the
controller still shows as online in the provider's web UI.

## 2. Deploy

The image is `ghcr.io/tabisch/estyma-local:latest` (amd64, arm64, armv7). It
contains both the proxy and the cloud recorder. `docker-compose.yml` defines:

- `estyma-proxy`: the proxy (host port 8088) and the web UI with the live monitor and log downloads (host port 8089)
- `cloud-recorder`: polls the provider's web API; idles if no credentials are set
- `estyma-logs`: named volume shared by both

Before deploying, check `UPSTREAM` in `docker-compose.yml`: it is where the
proxy forwards to. Because the NAT rule only matches the controller, the
provider's normal hostname works there.

### Portainer

1. *Stacks → Add stack*. Either paste `docker-compose.yml` into the web
   editor, or choose *Repository* with this repo's URL, reference
   `refs/heads/main` and compose path `docker-compose.yml`.
2. Under *Environment variables* add what you need (see the table below),
   typically `ESTYMA_EMAIL` and `ESTYMA_PASSWORD` for the cloud recorder and
   `UI_USER` / `UI_PASSWORD` to protect the log UI.
3. *Deploy the stack*.
4. To update after a new image was built: open the stack, *Update the stack*
   with *Re-pull image and redeploy* enabled (or *Pull and redeploy* for a
   repository stack).

If the GHCR package is private, add GHCR under *Registries* (username = your
GitHub user, password = a token with `read:packages`), or make the package
public in its GitHub package settings.

### docker compose

```sh
cp .env.example .env          # fill in credentials, ports, UI login
docker compose pull
docker compose up -d
docker compose logs -f estyma-proxy     # one line per captured exchange
```

## 3. Monitor and logs web UI

Open `http://<proxy_host>:8089/`.

- **Monitor** (`/`): the values the controller reports to the cloud,
  decoded from the proxied traffic and refreshed every 10 s: boiler, buffer,
  heating circuit, outside and flue gas temperatures, O2, burner power and
  state, boiler pump, lambda PWM, fuel counter, the reported setpoints with
  their limits, and the connection state (last exchange, cloud status, last
  value the cloud wrote to the controller). Charts cover the last 1 to 48
  hours; click a name in the temperature chart's legend to hide or show its
  line. Values outside a plausible range (corrupted CAN reads) are dropped.
  The history is kept in memory and rebuilt from the logs in the volume when
  the container starts. The same data is available as JSON at
  `/api/monitor?hours=24`.
- **Logs** (`/logs`): every log file in the volume with size and date, a
  download link per file and a "Download all (zip)" button. Read-only.

The UI is available in English and German. It follows the browser language;
the EN / DE switch at the top right overrides it and is remembered in the
browser. To add a language, copy the `en` block of `I18N` in
`proxy/monitor.html`, translate it and add a button to `#lang`.

The logs contain whatever the controller sends (device IDs, PINs, ...). Keep
port 8089 reachable from your LAN only and set `UI_USER` / `UI_PASSWORD`
(they protect the monitor too).

To analyse the logs inside the container instead:

```sh
docker exec <proxy-container> python3 /app/summarize_logs.py /logs          # endpoints and parameters
docker exec <proxy-container> python3 /app/compare/compare_cloud_proxy.py /logs  # match cloud values to registers
```

Register meanings found so far with this comparison are in
[docs/register-mapping.md](docs/register-mapping.md). The Polish cloud field
names, their English meaning and enum values are in
[docs/cloud-translation.md](docs/cloud-translation.md).

## 4. Operating modes (local server)

The proxy can also act as the controller's server. `MODE` picks how much it
does; the defaults keep the plain logging proxy and never write anything.

| `MODE` | Who answers the controller | Local writes |
|---|---|---|
| `proxy` (default) | the cloud, forwarded unchanged | no |
| `hybrid` | the cloud, but a pending local write is added to its reply | yes, if enabled |
| `local` | this server, the cloud is not contacted (unless `CLOUD_MIRROR=1`) | yes, if enabled |

Writes are read-only by default and limited to what has been seen working
from the cloud:

| Control | Register | Values | Needs |
|---|---|---|---|
| Boiler target | `8195,1` | whole °C, 60..80 and within the controller's own min/max | `WRITES_ENABLED=1` |
| Boiler on/off | `8197,1` | 0 / 1 | `WRITES_ENABLED=1` and `ALLOW_POWER_WRITES=1` |

How a write works:

- Set it on the **Control** panel of the monitor page (`:8089`). Writing from
  the page also needs `UI_USER` / `UI_PASSWORD`, so nobody on the LAN can
  change the heating without logging in.
- The write is sent like the cloud does it, `ZAPIS&;8195,1=62;4112,1=1;&KONIEC`,
  on every poll until the controller reports the new value (read-back), then
  it is done. If the value does not show up within `WRITE_TIMEOUT` seconds
  the write is marked failed. The panel always shows the reported value, not
  the requested one.
- One register per poll, at most one pending write per register (a new
  request replaces it), and at most one request per register every
  `WRITE_MIN_INTERVAL` seconds.
- While an alarm is active only on/off is sent (switching off and on is how
  an alarm is reset).
- In `hybrid` mode the cloud stays in charge: nothing is added to a reply
  that already carries a cloud write, and a cloud write to the same register
  cancels the local one. Changes made locally are seen by the cloud on the
  next report, like changes made at the controller.
- Every step (requested, sent, confirmed, failed, rejected, ...) is written
  to `writes-YYYY-MM-DD.jsonl` in the logs volume and listed on the page.

`local` mode answers every `/device/connect` itself with the same reply
layout as the cloud: the register list (learned from the cloud while running
in `proxy`/`hybrid` mode and saved in `state.json`, or a built-in default)
plus the two newest alarm slots, and a clock sync (`8549`, local time in
`CONTROLLER_TZ`) on start and every `CLOCK_SYNC_HOURS`. The vendor app no
longer gets data unless `CLOUD_MIRROR=1`, which still forwards each report
to the cloud in the background and ignores its reply (its writes are not
applied).

Suggested order: run `hybrid` with `WRITES_ENABLED=1` for a while and try a
boiler target change from the page; then `local`. To go back to the cloud at
any time set `MODE=proxy` (or remove the router's DNAT rule; the controller
then talks to the cloud directly). With `LOCAL_FALLBACK=1`, `proxy` and
`hybrid` answer locally whenever the cloud does not, so a flaky cloud no
longer leaves the controller without an answer.

The panel's data is also available as JSON: `GET /api/control`, and
`POST /api/set` with `{"key": "boiler_target", "value": 62}` (or
`boiler_on`, 0/1) and `POST /api/cancel` with `{"key": ...}`; both need
`Content-Type: application/json` and basic auth.

## Environment variables

Values below are defaults or placeholders.

### Proxy (`estyma-proxy` service, default command of the image)

| Variable           | Default in image | Meaning |
|--------------------|------------------|---------|
| `UPSTREAM`         | (required)       | Provider server the requests are forwarded to, e.g. `http://203.0.113.10:80` |
| `UPSTREAM_TIMEOUT` | `30`             | Seconds to wait for the provider before answering `502` |
| `LISTEN_HOST`      | `0.0.0.0`        | Bind address inside the container |
| `LISTEN_PORT`      | `80`             | Proxy port inside the container (the host port is `PROXY_PORT`) |
| `LOG_DIR`          | `/logs`          | Where the JSONL logs are written (the `estyma-logs` volume). |
| `MAX_BODY_LOG`     | `0`              | Max bytes of each body kept in the log, `0` = unlimited |
| `UI_PORT`          | `8089`           | Web UI (monitor + logs) port inside the container, `0` = UI off |
| `UI_USER`          | (unset)          | Basic auth user for the log UI |
| `UI_PASSWORD`      | (unset)          | Basic auth password; auth is on only when user and password are both set |
| `MONITOR_HISTORY_HOURS` | `48`        | Hours of decoded values the monitor keeps in memory for its charts (reloaded from the logs on start) |

### Local server (same service, see "Operating modes")

| Variable           | Default | Meaning |
|--------------------|---------|---------|
| `MODE`             | `proxy` | `proxy`, `hybrid` or `local` |
| `WRITES_ENABLED`   | `0`     | `1` = allow local writes (boiler target) |
| `ALLOW_POWER_WRITES` | `0`   | `1` = also allow switching the boiler on/off; needs `WRITES_ENABLED=1` |
| `WRITE_TIMEOUT`    | `120`   | Seconds until a write without read-back is marked failed |
| `WRITE_MIN_INTERVAL` | `10`  | Minimum seconds between two requests for the same register |
| `LOCAL_FALLBACK`   | `0`     | `1` = in `proxy`/`hybrid`, answer locally when the cloud fails instead of `502` |
| `CLOUD_MIRROR`     | `0`     | `1` = in `local`, still forward each report to the cloud and ignore its reply |
| `CONTROLLER_TZ`    | `Europe/Warsaw` | Time zone of the controller clock, for the clock sync in `local` mode |
| `CLOCK_SYNC_HOURS` | `6`     | Hours between clock syncs in `local` mode, `0` = off |
| `POLL_DELAY`       | `10`    | `DELAY` value in local replies (the controller's pause between polls) |
| `STATE_FILE`       | `/logs/state.json` | Last reported registers and the learned register list, kept across restarts |

`UPSTREAM` is not needed in `local` mode unless `CLOUD_MIRROR` or
`LOCAL_FALLBACK` is on.

### Cloud recorder (`cloud-recorder` service)

The service is always created. If `ESTYMA_EMAIL` / `ESTYMA_PASSWORD` are
missing it logs `cloud recorder disabled` and idles; set both and redeploy to
start recording.

| Variable          | Default in image | Meaning |
|-------------------|------------------|---------|
| `ESTYMA_EMAIL`    | (required)       | Login e-mail of your Estyma cloud account |
| `ESTYMA_PASSWORD` | (required)       | Password of that account |
| `ESTYMA_PASSWORD_B64` | (empty)      | Base64 of the password, overrides `ESTYMA_PASSWORD`. Use it when the password starts or ends with a space, because `.env` files and some UIs trim those (`printf ' my password' \| base64`) |
| `ESTYMA_PASSWORD_FILE` | (empty)     | Path to a file holding the password (e.g. a Docker secret); only a trailing newline is removed |
| `ESTYMA_DOMAIN`   | `igneo.pl`       | Which Estyma cloud to log in to |
| `ESTYMA_LANGUAGE` | `english`        | Language for the wrapper's value names |
| `INTERVAL`        | `30`             | Seconds between polls |
| `LOG_DIR`         | `/logs`          | Where `cloud-YYYY-MM-DD.jsonl` is written (same volume as the proxy) |
| `IDLE_IF_UNCONFIGURED` | (unset; `1` in compose) | Idle instead of exiting when credentials are missing |

### Compose / `.env` (host side)

`docker-compose.yml` reads these from the stack's *Environment variables*
(Portainer) or from `.env` (docker compose CLI) and passes them on:

| Variable          | Default | Meaning |
|-------------------|---------|---------|
| `PROXY_PORT`      | `8088`  | Host port of the proxy; the router's DNAT rule points here |
| `UI_PORT`         | `8089`  | Host port of the web UI (inside the container it stays 8089) |
| `UI_USER` / `UI_PASSWORD` | (empty) | Passed to the proxy, see above |
| `MODE`, `WRITES_ENABLED`, `ALLOW_POWER_WRITES`, `LOCAL_FALLBACK`, `CLOUD_MIRROR`, `CONTROLLER_TZ` | see above | Passed to the proxy |
| `ESTYMA_EMAIL` / `ESTYMA_PASSWORD` | (empty) | Passed to the cloud recorder, see above |

`UPSTREAM`, `UPSTREAM_TIMEOUT`, `ESTYMA_DOMAIN` and `INTERVAL` are set
directly in `docker-compose.yml`; edit them there.

## Image build

`.github/workflows/docker.yml` builds the image for amd64, arm64 and armv7 and
pushes it to `ghcr.io/tabisch/estyma-local` (tags `latest` and the commit SHA,
plus the tag name for `v*` tags). It builds when:

- a push to `main` contains a commit whose message includes `[build]`, e.g.
  `git commit -m "Add register decoding [build]"`; other pushes are skipped,
- a `v*` tag is pushed,
- it is started by hand: *Actions → Docker image → Run workflow*.

| Repository variable | Default | Meaning |
|---------------------|---------|---------|
| `RUNNER`            | (unset) | Unset = build on GitHub's runners; `self-hosted` = build on your own runner |

### Removing old images

Every build adds a version to the GHCR package. *Actions → Clean up images →
Run workflow* deletes all versions except the image tagged `latest` (its
platform images stay). It starts as a dry run that only lists what it would
delete; untick *Only list what would be deleted* to really delete.

### Building on your own GitHub Actions runner

By default the workflow runs on GitHub's hosted runners. To run it on your
own machine instead:

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

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use synthetic messages in the recorded layout (no real device
data) and run the proxy against a fake cloud.

## Log format

One JSON object per line in `exchanges-YYYY-MM-DD.jsonl` in the `estyma-logs` volume (UTC dates):

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
exchanges have an `error` field instead of `response`. Exchanges also carry
`mode`, and `local: true` (answered locally), `injected` (local writes added
to the cloud's reply) or `mirror: true` (background copy to the cloud in
`local` mode) where that applies.

Logs contain whatever the controller sends, possibly including account
credentials or device IDs. Keep them private and out of git.

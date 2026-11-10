# Development

## Code layout

| Path | What it is |
|---|---|
| `proxy/estyma_proxy.py` | Entry point: the HTTP server the controller talks to (proxy / hybrid / local), wires everything together |
| `proxy/protocol.py` | Parsing and building the controller's messages (`ZAPISZ_ODCZYT`, `ZAPIS`, `ODCZYT`) |
| `proxy/controller.py` | Controller state, settings (`WRITABLE`) and the safe write queue |
| `proxy/catalog.py` | **Everything known about the registers**: labels, decoding, ranges, value names, alarm texts |
| `proxy/config.py` | **Every environment variable** with default and description |
| `proxy/monitor.py` | Decoded values and history for the monitor page and MQTT |
| `proxy/registers.py` | Raw register table for `/registers` |
| `proxy/log_ui.py` + `*.html` | Web UI on port 8089 |
| `proxy/mqtt_publisher.py` | MQTT publisher with Home Assistant discovery |
| `proxy/cloud_monitor.py` | Optional polling of the Estyma cloud |
| `compare/` | Analysis tools: cloud recorder, cloud vs. register comparison |
| `tools/gendocs.py` | Generates `docs/registers.md`, `docs/configuration.md` and `.env.example` |
| `tests/` | Unit tests (synthetic data only) |

### Generated files

`docs/registers.md`, `docs/configuration.md` and `.env.example` are generated
from `proxy/catalog.py` and `proxy/config.py`; do not edit them by hand.
After changing a register or an environment variable run:

```sh
python3 tools/gendocs.py
```

`tests/test_docs.py` fails when a generated file is out of date, when the code
reads an environment variable that is not in `proxy/config.py`, or when
`docker-compose.yml` does not pass on a variable marked `compose` with the
same default.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use synthetic messages in the recorded layout (no real device
data) and run the proxy against a fake cloud. The MQTT test also connects to a
real broker when `mosquitto` is installed (otherwise that part is skipped).


## Image build

`.github/workflows/docker.yml` builds the image for amd64 (other architectures are switched off for now) and
pushes it to `ghcr.io/tabisch/estyma-local` (tags `latest` and the commit SHA,
plus the tag name for `v*` tags). It builds when:

- a push to `main` contains a commit whose message includes `[build]`, e.g.
  `git commit -m "Add register decoding [build]"`; other pushes are skipped,
- a `v*` tag is pushed,
- it is started by hand: *Actions → Tests and Docker image → Run workflow*.

Before every build, and on every push to `main`, tag and pull request, the
workflow runs the unit tests (see [Tests](#tests)). The image is only built and pushed
when they pass. Pull requests only run the tests and never push an image.

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
4. Re-run the workflow (*Actions → Tests and Docker image → Run workflow*).

The build is amd64 only, so use an x86-64 machine as the runner. To build
for ARM again, add the platforms in `docker.yml` and put back the
`docker/setup-qemu-action` step (see the comment there).

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

# estyma-local

A local replacement for the Estyma heating system's cloud server. The
controller talks to the provider over unencrypted HTTP; this container sits
in between and can

- log every request and reply (transparent proxy, the default),
- show the controller's values in a local **web UI** (monitor, raw registers,
  heating settings),
- write settings to the controller itself, add its own writes to the cloud's
  replies, or answer the controller without the cloud at all
  (**operating modes** `proxy`, `hybrid`, `local`),
- publish everything to **Home Assistant** over MQTT,
- poll the Estyma cloud for comparison (**cloud monitoring**, optional).

Setup is in [docs/setup.md](docs/setup.md), everything else in [docs/](docs/README.md).

## How it works

```
                      firewall DNAT
heating controller --- provider_ip:80 ---> proxy_host:8088 (estyma-proxy) ---> provider server
                                                  |
                                                  +--> volume estyma-logs
                                                          exchanges-YYYY-MM-DD.jsonl
                                                  |
                                   browser ---> proxy_host:8089 (monitor + log download UI)
```

- Request headers are forwarded verbatim (order and casing kept, including
  `Host`), bodies byte for byte. Only hop-by-hop headers (`Connection`,
  `Transfer-Encoding`, ...) are normalised.
- If the provider is unreachable the controller gets a `502` and the attempt
  is still logged.

## Setup

1. Redirect the controller's traffic to the proxy host: a firewall NAT
   rule (recommended) or a DNS rewrite (untested).
2. Deploy the container as a Portainer stack or with docker compose.

Both steps are described in [docs/setup.md](docs/setup.md).

## Use it

Open `http://<proxy_host>:8089/`. Then, depending on what you want:

| Topic | Doc |
|---|---|
| Web UI: monitor, registers, heating settings, logs | [docs/web-ui.md](docs/web-ui.md) |
| Writing settings, running without the cloud | [docs/operating-modes.md](docs/operating-modes.md) |
| Home Assistant via MQTT | [docs/home-assistant.md](docs/home-assistant.md) |
| Comparing with the Estyma cloud | [docs/cloud-monitoring.md](docs/cloud-monitoring.md) |
| All environment variables | [docs/configuration.md](docs/configuration.md) |
| All known registers | [docs/registers.md](docs/registers.md) |
| Code layout, tests, image build, log format | [docs/development.md](docs/development.md) |

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

This README covers the setup: Docker (Portainer stack or docker compose) plus
a NAT rule on the firewall/router. Everything else is in [docs/](docs/README.md).

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

The image is `ghcr.io/tabisch/estyma-local:latest` (amd64 only for now). It
contains the proxy, the web UI and the optional cloud monitoring. `docker-compose.yml` defines:

- `estyma-proxy`: the proxy (host port 8088) and the web UI with the live monitor and log downloads (host port 8089)
- `estyma-logs`: named volume for the logs

Before deploying, check `UPSTREAM` in `docker-compose.yml`: it is where the
proxy forwards to. Because the NAT rule only matches the controller, the
provider's normal hostname works there.

### Portainer

1. *Stacks → Add stack*. Either paste `docker-compose.yml` into the web
   editor, or choose *Repository* with this repo's URL, reference
   `refs/heads/main` and compose path `docker-compose.yml`.
2. Under *Environment variables* add what you need (see
   [docs/configuration.md](docs/configuration.md)),
   typically `UI_USER` / `UI_PASSWORD` to protect the log UI, and
   `ESTYMA_EMAIL` / `ESTYMA_PASSWORD` if you want cloud monitoring.
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

## 3. Use it

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

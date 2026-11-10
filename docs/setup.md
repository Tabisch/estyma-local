# Setup

This covers one setup: Docker (Portainer stack or docker compose) plus a
way to redirect the controller's traffic to the container. All settings are
environment variables, listed in [configuration.md](configuration.md).

## 1. Redirect the controller's traffic

The controller always connects to the provider's server on port 80. That
traffic has to reach the proxy instead. The firewall NAT rule below is the
method in use; a DNS rewrite is described after it as an alternative.

### Firewall NAT rule (recommended)

A DNAT rule on the firewall/router sends the controller's traffic for
`provider_ip:80` to the proxy:

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

### DNS rewrite (alternative, untested)

If the controller looks up the provider's host name (`igneo.pl`) through your
DNS server, a local DNS override can point that name at the proxy host
instead, e.g. a *Local DNS record* in Pi-hole / AdGuard Home, a host override
in OPNsense/pfSense, or `address=/igneo.pl/<proxy_host>` in dnsmasq. Things
to watch:

- It only works if the controller uses DNS at all and gets its DNS server
  from your network (DHCP). If it has a fixed IP address built in, the
  rewrite does nothing; use the NAT rule.
- The controller still connects to port 80, so the proxy must be reachable
  on port 80 of the proxy host: set `PROXY_PORT=80` (the port must be free).
- The proxy itself must not resolve the rewritten name, or it would forward
  to itself. Set `UPSTREAM` in `docker-compose.yml` to the provider's IP
  address (`http://<provider_ip>:80`) instead of the host name.
- The override applies to every device that uses this DNS server, so the
  vendor app and browsers on the LAN would reach the proxy too, unless the
  DNS server can limit the rule to the controller's IP.

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
   [docs/configuration.md](configuration.md)),
   typically `UI_USER` / `UI_PASSWORD` to protect the log UI, and
   `ESTYMA_EMAIL` / `ESTYMA_PASSWORD` if you want cloud monitoring.
3. *Deploy the stack*.
4. To update after a new image was built: open the stack, *Update the stack*
   with *Re-pull image and redeploy* enabled (or *Pull and redeploy* for a
   repository stack).

### docker compose

```sh
cp .env.example .env          # fill in credentials, ports, UI login
docker compose pull
docker compose up -d
docker compose logs -f estyma-proxy     # one line per captured exchange
```

Next: open `http://<proxy_host>:8089/`, see [web-ui.md](web-ui.md).

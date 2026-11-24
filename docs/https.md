# HTTPS for the web UI

The web UI speaks plain HTTP on port 8089. To reach it over HTTPS (on the
LAN, or from your phone through a VPN), put a reverse proxy in front of it.
The proxy does TLS; estyma-local stays on HTTP behind it.

Checklist:

1. Keep the login on (the default) and set `UI_PASSWORD` (see
   [web-ui.md](web-ui.md#login)).
2. Set `UI_TRUST_PROXY=1`, so the failed-login limit sees the real client
   address (from `X-Forwarded-For`) instead of the proxy's.
3. Make port 8089 reachable **only through the proxy**: otherwise anyone
   could connect directly and send a fake `X-Forwarded-For`. With the proxy
   on the same host, publish the port on localhost only, in
   `docker-compose.yml`: `- "127.0.0.1:${UI_PORT:-8089}:8089"`. With the
   proxy on another host, let the firewall allow 8089 from that host only.
4. The proxy must pass on the original `Host` header and send
   `X-Forwarded-Proto: https`. With it the session cookie is marked
   `Secure`. The server refuses POSTs whose `Origin` does not match the
   `Host` (or `X-Forwarded-Host`), so a proxy that rewrites `Host` breaks
   every button.
5. Do not expose the UI to the internet. Use a VPN (WireGuard, Tailscale,
   your router's VPN) to reach it from outside.

The controller port (`PROXY_PORT`, 8088) stays plain HTTP: the controller
only speaks HTTP to its server.

## Caddy

Caddy gets and renews certificates itself and sends the right headers by
default. With a name in your local DNS and Caddy's own CA (install its root
certificate on your devices, `caddy trust`):

```
heating.home.arpa {
    tls internal
    reverse_proxy 127.0.0.1:8089
}
```

With a real domain whose DNS you control, Caddy can get a public
certificate with the DNS challenge (needs the DNS plugin for your
provider), without opening any port to the internet.

## nginx

```nginx
server {
    listen 443 ssl;
    server_name heating.home.arpa;
    ssl_certificate     /etc/nginx/certs/heating.crt;
    ssl_certificate_key /etc/nginx/certs/heating.key;

    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
server {
    listen 80;
    server_name heating.home.arpa;
    return 301 https://$host$request_uri;
}
```

## Home Assistant add-ons and Traefik

The *NGINX Home Assistant SSL proxy* add-on and Traefik both keep `Host`
and send `X-Forwarded-*` by default; point them at `http://<docker host>:8089`.

## MQTT

The MQTT publisher can use TLS to the broker as well: `MQTT_TLS=1` and
usually `MQTT_PORT=8883` (see [home-assistant.md](home-assistant.md)).

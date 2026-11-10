# Cloud monitoring

The proxy can also poll the Estyma cloud (through the
[EstymaApi wrapper](https://github.com/Tabisch/EstymaApi)) and write what the
cloud reports to `cloud-YYYY-MM-DD.jsonl` in the logs volume, next to the
proxy logs, for `compare_cloud_proxy.py`. It is switched on and off on the
*Settings* page (`/settings`), which also shows the state (off, logging in,
running, problem) and the time of the last poll. While it is on, the monitor
page has a *Cloud monitoring* section with the latest cloud values next to
the ones the controller reported locally.

- The credentials only come from the container environment:
  `ESTYMA_EMAIL` plus `ESTYMA_PASSWORD` (or `ESTYMA_PASSWORD_B64` /
  `ESTYMA_PASSWORD_FILE`). The web UI cannot see, enter or store them.
  Without them the settings page shows *Not configured* and the button is
  disabled.
- Switching needs `UI_USER` / `UI_PASSWORD`, like deleting logs.
- The choice is saved in `cloud-monitor.json` in the logs volume and kept
  across restarts and image updates. Until it is first switched in the UI,
  `CLOUD_MONITOR` decides (default `0` = off).
- Switching off logs out of the cloud and stops polling right away.
- The cloud login is retried with a growing delay (30 s up to 15 min) when
  it fails; the reason is shown on the settings page and the monitor page.

As JSON: `GET /api/cloud`, and `POST /api/cloud` with `{"enabled": true}` or
`false` (`Content-Type: application/json`, basic auth).

The wrapper is installed into the image automatically from
`requirements.txt` (pinned version); nothing to install by hand.

The standalone recorder `compare/cloud_recorder.py` is still in the image
for one-off runs (`docker exec <proxy-container> python3
/app/compare/cloud_recorder.py --count 1` with the same variables).

All variables are listed in [configuration.md](configuration.md#cloud-monitoring).

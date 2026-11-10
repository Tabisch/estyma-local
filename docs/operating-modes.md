# Operating modes (local server)

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
| Every other setting on the heating settings page | see [registers.md](registers.md) | the cloud panel's range, within the controller's own min/max | `WRITES_ENABLED=1` and `ALLOW_UNTESTED_WRITES=1` |

Only `8195` and `8197` were seen being written on the real controller. The
other settings use the same `ZAPIS` path and the cloud panel's ranges, but
are marked *untested* on the page and stay locked unless
`ALLOW_UNTESTED_WRITES=1`. Change them one at a time and check the result on
the controller's own screen. Air correction (`8819`) is limited to 0..40 %
because the encoding of negative values is not known; weekly timers are not
offered yet.

The settings are not part of the controller's normal report. In `hybrid` and
`local` mode the server adds them to the register list (`ODCZYT`), the same
way the cloud's service panel reads them: on start, every
`SETTINGS_READ_MINUTES` and when *Read settings now* is pressed. A register
with a pending write is added on every poll so its read-back can confirm it.
In `proxy` mode the page is read only and shows whatever the cloud last
read.

How a write works:

- Set it on the **Heating settings** page (`:8089/heating`). Writing from
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

## API

The write state is also available as JSON: `GET /api/control`, and
`POST /api/set` with `{"key": "boiler_target", "value": 62}` (or
`boiler_on`, 0/1) and `POST /api/cancel` with `{"key": ...}`; both need
`Content-Type: application/json` and basic auth.

All variables mentioned here are listed in [configuration.md](configuration.md).

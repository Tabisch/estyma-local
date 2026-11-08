# Register <-> cloud field mapping

From `compare_cloud_proxy.py` runs by Tabisch on 2026-11-08: first over
2026-11-07 19:41 to 2026-11-08 00:59 (636 aligned samples), then over
2026-11-07 19:41 to 2026-11-08 08:27 (1532 aligned samples, includes a
controller off/on and a circuit 1 pump cycle). Register format `reg,idx` as in `ZAPISZ_ODCZYT`.

Main finding: the cloud field suffix `_sub1` / `_sub3` / `_sub4` is the
register index 1 / 3 / 4 (value / min / max). Every field that changed
confirms this, so it is used below to settle fields whose value stayed constant.

## Confirmed (value changed during the recording, about 100 % match)

| Register | Transform | Cloud field (raw / wrapper name) | Range seen |
|---|---|---|---|
| 8193,1 | /10 | `kociol_temp_sub1` / temp_boiler_sub1 | 64.5..80.5 C |
| 8466,1 | /10 | `bufor_temp_gor_sub1` / temp_buffer_top_sub1 | 55.6..75.2 C |
| 8467,1 | /10 | `bufor_temp_dol_sub1` / temp_buffer_bottom_sub1 | 49.3..68.5 C |
| 16385,1 | /10 | `temp_co1_sub1` / temp_heating_curcuit1_sub1 | 51.2..61.7 C |
| 8543,1 | /10 | `temp_zewnetrzna_sub1` / temp_outside_sub1 | 7.1..8.1 C |
| 8473,1 | x1 (low byte) | `kociol_temp_spalin_sub1` / temp_exhaust_boiler_sub1 | 46..113 C (flue gas, NOT fuel level as guessed earlier) |
| 8480,1 | /100 | `spaliny_tlen_sub1` / oxygen_content_exhaust_sub1 | 0..20.95 % O2 |
| 16389,1 | x1 (low byte) | `moc_palnika_sub1` / power_output_boiler_sub1 | 0..75 % burner power |
| 16391,1 | low byte | `lambda_pwm_sub1` / lamda_pwm_sub1 | 0..100 |
| 8199,1 | low byte | `palnik_status_sub1_int` / current_status_burner_sub1_int | 0..10 burner state |
| 8823,1 | low byte | `kociol_stan_pompy_sub1` / status_boiler_pump_sub1 | 0/1 |
| 8196,1 | x1 | `kociol_temp_obli_sub1` / temp_boiler_obli_sub1 | 7..80 calculated boiler target |
| 8197,1 | low byte | `on_off_sub1` / status_controller_sub1 | 0/1 controller on |
| 8857,1 | low byte | `co1_stan_pompy_sub1` / status_pump_heating_curcuit1_sub1 | 0/1 circuit 1 pump |
| 8484,1 | x1 (cloud drops last digit) | `licznik_paliwa_sub1` / consumption_fuel_total_current_sub1 | fuel counter |

Low-byte matches are `0x100 + value` style registers.

Burner status values seen (`8199,1` low byte, cloud text in German UI):
0 off (ausgeschaltet), 1 ignition (anzünden), 2 glowing (glühend),
5 modulation, 7 cleaning (Reinigung), 10 error (Fehler).

## Alarms

Found from a "missing fuel" alarm on 2026-11-08 (three alarms, controller
switched off/on in between to reset).

| Register | Meaning |
|---|---|
| 16460,1 | Alarm counter, `0x100 + n`; +1 on every new alarm (268 -> 269 -> 270 -> 271) |
| 16411 + k, 1 | Alarm slot k: state in bits 16+ and alarm code in the low byte. `0x3008C` = active, `0x7008C` = being reset, `0x8C` = cleared. Code 140 (0x8C) = missing fuel / ignition failure |
| 16431 + k, 1 | Alarm slot k start time (Unix time, controller clock) |
| 16451 + k, 1 | Alarm slot k end time; 0 while the alarm is active |

The slot that is used follows the counter: counter 268 -> slot 16411/16431/16451
(an older code-140 alarm from 2026-11-05), 269 -> 16412/..., 270 -> 16413/...,
271 -> 16414/.... When the counter goes up the cloud adds the new slot's three
registers to its `ODCZYT` request list (63 -> 66 entries) and drops older ones
later, so it only ever asks for the latest one or two slots.

The alarm timestamps are about 45 minutes behind the real time even though the
cloud syncs `8549` (controller clock, local time) every few minutes; the cause
is unknown.

While an alarm is active, the cloud's `online.ilosc_alarmow` is 1 and the
burner status is 10.

## Read glitches

Corrupted readings have garbage in the upper 16 bits and the correct value in
the lower 16 bits (e.g. `31981648` = `0x01E80050` for 80, `4653063` =
`0x00470007` for 7), or are `0xA5A5....`, `1000039936 + x` or `100794368 + x`.
They last for one report. A local server should drop a value that jumps for a
single report, or keep only the low 16 bits for registers that never exceed
them.

## Settings (constant during recording, single fit or fixed by the sub->idx rule)

| Register | Transform | Cloud field | Value |
|---|---|---|---|
| 8195,1 / ,3 / ,4 | x1 | `kociol_temp_zad_sub1/3/4` boiler target / min / max | 60 / 60 / 80 |
| 8468,1 / ,3 / ,4 | x1 | `bufor_temp_zad_gor_sub1/3/4` buffer top target / min / max | 50 / 7 / 80 |
| 8469,1 / ,3 / ,4 | x1 | `bufor_temp_zad_dol_sub1/3/4` buffer bottom target / min / max | 65 / 7 / 80 |
| 8242,1 / ,3 / ,4 | /10 | `tpok_komf_obw1_sub1/3/4` room comfort temp / min / max | 21 / 15 / 30 |
| 8264,1 / ,3 / ,4 | /10 | `tpok_eko_obw1_sub1/3/4` room eco temp / min / max | 18 / 10 / 30 |
| 8194,1 | x1 | `kociol_tryb_sub1` / operation_mode_boiler_sub1 | 2 |
| 8485,1 | x1 | `typ_urzadzenia_sub1` / device_type_sub1 | 1000040002 |
| 8486..8510 step 4 | x1 | `co1_timer_pn..n_sub1` weekly timers circuit 1 | 4194240 each; order not proven, probably Mon=8486, Tue=8490, Wed=8494, Thu=8498, Fri=8502, Sat=8506, Sun=8510 |
| 8292,1 (probably) | low byte | `co_prog_obw1_sub1` heating circuit program | 3 |

## Still ambiguous

- Fields with value 1 (`lambda_tak_nie_sub1` lambda fitted,
  `liczba_buforow_sub1` buffers, `liczba_obw_co_sub1` heating circuits):
  configuration, one of the remaining 0x101 flag registers each (8198,
  8453..8456, 8483, 8548, 8720, 8865, ...). They will not change in normal use.
- `consumption_fuel_current_day` is computed by the wrapper (fuel counter minus
  `daystats_data.pierwszy_pomiar_paliwa`, the counter at midnight); the
  midnight value is cloud-side and has no register.
- 16412..16414, 16432..16434, 16452..16454 are alarm slots, see "Alarms".
- Fields with value 0 (circuits 2..4 temps, return temp, lambda temp,
  solar, fuel fill level, energy meter, DHW): not present / not fitted on
  this installation; they match any zero register.
- Timer weekday order and `co_prog_obw1_sub1` (see above).

## Changes in the web UI that would settle the rest

Run proxy and recorder, make one change at a time, wait ~2 min, change back:
1. Boiler target 60 -> 61 (separates 8195,1 from 8195,3; confirms writes reach the controller). Not done yet as of the second run.
2. Monday timer of heating circuit 1 (one hour) -> confirms weekday order.
3. Heating circuit program (`co_prog_obw1`) to another value -> confirms 8292.
4. Optional: circuit 1 pump / controller on-off flags, only if acceptable to switch heating briefly.

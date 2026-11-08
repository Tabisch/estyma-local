# Estyma cloud field translation

The igneo.pl cloud (`POST /info_panel_update`) answers with Polish field names.
This table gives the literal meaning of each Polish key, a suggested English
name, the name the EstymaApiWrapper (0.0.70) uses today, and the controller
register from [register-mapping.md](register-mapping.md).

Suffixes: `_sub1` / `_sub3` / `_sub4` are register index 1 / 3 / 4, which is
**value / minimum / maximum** (confirmed, see register-mapping.md). Names below
use `_min` / `_max` for `_sub3` / `_sub4`.

Polish abbreviations used in the keys:

| Polish | Full form | Meaning |
|---|---|---|
| `co` | centralne ogrzewanie | central heating (a heating circuit) |
| `cwu` | ciepła woda użytkowa | domestic hot water (DHW) |
| `obw` | obwód | circuit |
| `tpok` | temperatura pokojowa | room temperature |
| `zad` | zadana | set point, target |
| `obli` | obliczona | calculated |
| `gor` / `dol` | góra / dół | top / bottom |
| `komf` / `eko` | komfortowa / ekonomiczna | comfort / economy |
| `tak_nie` | tak / nie | yes / no (a "fitted?" flag) |
| `pn wt sr cz pt so n` | poniedziałek … niedziela | Monday … Sunday |

## Live values and settings

✏ in the "Wrapper" column marks a wrapper name that is wrong or misleading.

| Cloud key | Literal meaning | Suggested English name | Wrapper name | Register | Notes |
|---|---|---|---|---|---|
| `kociol_temp_sub1` | boiler temperature | `temp_boiler` | temp_boiler_sub1 | 8193,1 /10 | |
| `kociol_temp_obli_sub1` | boiler temperature, calculated | `temp_boiler_calculated` | temp_boiler_obli_sub1 ✏ | 8196,1 | "obli" left untranslated. The target the controller is currently heating to (7 when idle). |
| `kociol_temp_zad_sub1/3/4` | boiler temperature, set point | `temp_boiler_target` / `_min` / `_max` | temp_boiler_target_sub1/3/4 | 8195,1/3/4 | |
| `kociol_temp_spalin_sub1` | boiler flue gas temperature | `temp_flue_gas` | temp_exhaust_boiler_sub1 | 8473,1 | "exhaust" is understandable; "flue gas" is the usual heating term. |
| `kociol_stan_pompy_sub1` | boiler pump state | `pump_boiler` (0/1) | status_boiler_pump_sub1 | 8823,1 low byte | |
| `kociol_tryb_sub1` | boiler mode | `mode_boiler` | operation_mode_boiler_sub1 | 8194,1 | Text value, see enums. |
| `temp_powrotu_sub1` | return temperature | `temp_return` | temp_boiler_return_sub1 | ? | 0 here (no return sensor). |
| `spaliny_tlen_sub1` | flue gas oxygen | `oxygen_flue_gas` (%) | oxygen_content_exhaust_sub1 | 8480,1 /100 | |
| `moc_palnika_sub1` | burner power | `burner_power` (%) | power_output_boiler_sub1 ✏ | 16389,1 | Power of the burner, not the boiler output. |
| `palnik_status_sub1` | burner status (text) | `burner_status_text` | current_status_burner_sub1 | 8199,1 | Text in the account's UI language, see enums. |
| `palnik_status_sub1_int` | burner status (number) | `burner_status` | current_status_burner_sub1_int | 8199,1 low byte | See enums. |
| `palnik_zgoda_na_prace_sub1` | burner permission to run | `burner_enabled` | burner_enabled_sub1 | ? | Text ("ja" = yes). |
| `lambda_pwm_sub1` | lambda probe PWM | `lambda_pwm` | lamda_pwm_sub1 ✏ | 16391,1 low byte | Typo "lamda". |
| `lambda_temp_sub1` | lambda probe temperature | `temp_lambda` | temp_lamda_sub1 ✏ | ? | Typo "lamda". 0 here. |
| `lambda_tak_nie_sub1` | lambda probe fitted (yes/no) | `lambda_installed` | state_lamda_sub1 ✏ | one of the 0x101 flags | Typo, and it is a "fitted" flag, not a state. |
| `licznik_paliwa_sub1` | fuel counter | `fuel_total` (kg) | consumption_fuel_total_current_sub1 | 8484,1 /10 | Cloud value has one decimal digit: 541570 = 54157.0 kg. |
| `ilosc_paliwa_zbiornik_sub1` | amount of fuel in the hopper | `fuel_level_hopper` | fuel_fill_level_sub1 | ? | 0 here (no level sensor). |
| `licznik_energii_sub1` | energy counter | `energy_total` | energy_meter_sub1 | ? | 0 here. |
| `temp_zewnetrzna_sub1` | outside temperature | `temp_outside` | temp_outside_sub1 | 8543,1 /10 | |
| `temp_co1_sub1` … `temp_co4_sub1` | central heating circuit 1…4 temperature | `temp_heating_circuit1` … `4` | temp_heating_curcuit1_sub1 … ✏ | 16385,1 /10 (circuit 1) | Typo "curcuit". Circuits 2–4 not fitted here. |
| `co1_stan_pompy_sub1` | circuit 1 pump state | `pump_heating_circuit1` (0/1) | status_pump_heating_curcuit1_sub1 ✏ | 8857,1 low byte | Typo "curcuit". |
| `typCzujnika_obw1_sub1` | sensor type, circuit 1 | `sensor_type_circuit1` | sensor_type_circuit1_sub1 | ? | |
| `tpok_obw1_sub1` | room temperature, circuit 1 | `temp_room_circuit1` | target_temp_obw1_sub1 ✏ | ? | **Measured** room temperature, not a target. "kein" (= none) means no room sensor is fitted. |
| `tpok_komf_obw1_sub1/3/4` | room comfort temperature, circuit 1 | `temp_room_comfort_circuit1` / `_min` / `_max` | target_temp_room_comf_heating_curcuit_sub1/3/4 ✏ | 8242,1/3/4 /10 | Typo, and the circuit number is lost. |
| `tpok_eko_obw1_sub1/3/4` | room economy temperature, circuit 1 | `temp_room_eco_circuit1` / `_min` / `_max` | target_temp_room_eco_heating_curcuit_sub1/3/4 ✏ | 8264,1/3/4 /10 | Same. |
| `co_prog_obw1_sub1` | heating program, circuit 1 | `program_heating_circuit1` | heating_curcuit_prog_obw1_sub1 ✏ | 8292,1 low byte (probably) | Typo, "obw1" left untranslated. Text value, see enums. |
| `co1_timer_pn_sub1` … `co1_timer_n_sub1` | circuit 1 timer, Monday … Sunday | `timer_heating_circuit1_monday` … `_sunday` | timer_heating_curcuit1_monday_sub1 … ✏ | 8486 … 8510 step 4 (order probable) | Weekday translation is right (pn, wt, śr, cz, pt, so, n); only the typo. See timers below. |
| `bufor_temp_gor_sub1` | buffer temperature, top | `temp_buffer_top` | temp_buffer_top_sub1 | 8466,1 /10 | |
| `bufor_temp_dol_sub1` | buffer temperature, bottom | `temp_buffer_bottom` | temp_buffer_bottom_sub1 | 8467,1 /10 | |
| `bufor_temp_zad_gor_sub1/3/4` | buffer top set point | `temp_buffer_top_target` / `_min` / `_max` | target_temp_buffer_top_sub1/3/4 | 8468,1/3/4 | |
| `bufor_temp_zad_dol_sub1/3/4` | buffer bottom set point | `temp_buffer_bottom_target` / `_min` / `_max` | target_temp_buffer_bottom_sub1/3/4 | 8469,1/3/4 | |
| `cwu_zagrzej_teraz_sub1` | DHW: heat now | `dhw_heat_now` | cwu_heat_now_sub1 ✏ | ? | "cwu" left untranslated. One-off hot water charge. |
| `on_off_sub1` | on / off | `controller_on` | status_controller_sub1 | 8197,1 low byte | Text value, see enums. |
| `typ_urzadzenia_sub1` | device type | `device_type` | device_type_sub1 | 8485,1 | 1000040002 here. |
| `liczba_obw_co_sub1` | number of central heating circuits | `count_heating_circuits` | number_obw_heating_curcuit_sub1 ✏ | one of the 0x101 flags | "obw" left untranslated, typo. |
| `liczba_obw_cwu_sub1` | number of DHW circuits | `count_dhw_circuits` | number_obw_cwu_sub1 ✏ | ? | "obw" and "cwu" left untranslated. |
| `liczba_buforow_sub1` | number of buffers | `count_buffers` | number_buffers_sub1 | one of the 0x101 flags | |
| `solary_tak_nie_sub1` | solar fitted (yes/no) | `solar_installed` | status_solar_connected_sub1 | ? | Fine, "installed" is closer to "tak/nie". |

## Day statistics (`daystats_data`) and online status (`online`)

These are computed by the cloud and have no register.

| Cloud key | Literal meaning | Suggested English name | Wrapper name | Notes |
|---|---|---|---|---|
| `daystats_data.rangeFrom` / `rangeTo` | range from / to | same | same | Start and end of the current day. |
| `daystats_data.solary_zebrana_moc` | solar: collected power | `solar_energy_collected` | solar_power_used ✏ | "zebrana" = collected/harvested, not used. |
| `daystats_data.pierwszy_pomiar_paliwa` | first fuel measurement | `fuel_total_day_start` | consumption_fuel_end_of_last_day ✏ | Fuel counter at the start of `rangeFrom`, same unit as `licznik_paliwa`. Close to "end of last day", but it is a counter reading, not a consumption. |
| `daystats_data.date` | date | same | same | Time of the last report (local time). |
| `online.is_online` | is online | same | same | |
| `online.last_date` | last date | `last_report` | last_date | Time of the controller's last report. |
| `online.diff` | difference | `last_report_age` | diff | Text like `0d 00h:0m:2s`. The wrapper adds `diffSeconds`. |
| `online.can_status` | CAN bus status | same | same | Same as `CAN STATUS=` in the controller's request (2 here). |
| `online.ilosc_alarmow` | number of alarms | `alarm_count` | number_of_alarms | Active alarms. 1 while the missing-fuel alarm was active. |
| `_execution_time` | | same | same | Cloud server timing, not device data. |

## Setting change states (`/info_check_order`)

| Cloud value | Meaning | Suggested | Wrapper |
|---|---|---|---|
| `nowy` | new | `new` | new |
| `wyslano` | sent | `sent` | shipped ✏ |
| `zrealizowano` | carried out | `completed` | completed |
| `blad` (błąd) | error | `failed` | failed |

## Enum values

The text values are in the language the web UI is set to (German for this
account, see wrapper issue 1 below), so a local server should use the numbers.

**Burner status** (`palnik_status_sub1_int`, register 8199,1 low byte):

| Value | Cloud text (German) | English |
|---|---|---|
| 0 | ausgeschaltet | off |
| 1 | anzünden | ignition |
| 2 | glühend | glowing (fire building up after ignition) |
| 5 | Modulation | modulating (normal operation) |
| 7 | Reinigung | cleaning |
| 10 | Fehler | error (alarm active) |

3, 4, 6, 8 and 9 were not seen.

**Other text values seen** (the number is what the wrapper's `textToValues` turns them into):

| Field | Cloud text | Value | English |
|---|---|---|---|
| `on_off_sub1` | EIN / AUS | 1 / 0 | on / off |
| `kociol_tryb_sub1` | Automatisch | 2 | automatic |
| `co_prog_obw1_sub1` | komfortable | 3 | comfort |
| `palnik_zgoda_na_prace_sub1` | ja | | yes |
| `tpok_obw1_sub1` | kein | | none (no room sensor) |

Other options of these lists come from the `<select>` elements on the device
page and have not been recorded yet.

**Alarm codes** (alarm slot registers, see register-mapping.md):

| Code | Meaning |
|---|---|
| 140 (0x8C) | missing fuel / ignition failed |

**Timers** (`co1_timer_*`): every day is `4194240` = `0x3FFFC0`, bits 6 to 21
set. This fits a 24-bit hour mask with comfort from 06:00 to 22:00 (bit n =
hour n). Not confirmed; changing one hour in the web UI would settle it.

## Suggested wrapper fixes

For EstymaApiWrapper 0.0.70. Renaming keys breaks existing users (the Home
Assistant integration), so the safest route is to add the corrected names and
keep the old ones as deprecated aliases for a while.

1. **Language switch never happens.** `switchLanguage()` requests `login_url`
   instead of `languageSwitch_url`, and `languageSwitch_url` has a stray brace
   (`"https://{0}/switchLanguage/{1}}"`). So text values arrive in whatever
   language the account uses (German here), not in `language`. Fix:
   `languageSwitch_url = "https://{0}/switchLanguage/{1}"` and use it in
   `switchLanguage()`.
2. **Fuel counter rounding and crash.** `int(str(value)[:-1])` drops the tenth
   instead of keeping it, and raises `ValueError` for values 0..9 (empty
   string). Use `value / 10` for `licznik_paliwa_sub1` and
   `pierwszy_pomiar_paliwa`.
3. **Translation by string replace.** `_translateApiOutput` replaces every key
   as a substring of the whole JSON text, so it also rewrites matching text
   inside values, and it depends on order (`palnik_status_sub1` is a prefix of
   `palnik_status_sub1_int`; it only works because the result happens to be the
   right name). It also `json.dumps()` a string that is already JSON. Translate
   keys while walking the dict instead.
4. **Typos:** `curcuit` → `circuit` (all heating circuit fields), `lamda` →
   `lambda`, `shipped` → `sent`.
5. **Wrong meaning:** `target_temp_obw1_sub1` is the measured room temperature
   of circuit 1 → `temp_room_circuit1`. `solar_power_used` → collected.
   `power_output_boiler` → burner power. `state_lamda` → lambda fitted.
6. **Untranslated parts and lost circuit numbers:** `obw`, `obli`, `cwu`, and
   `..._heating_curcuit_sub1` for `tpok_komf_obw1` / `tpok_eko_obw1` (the
   circuit number 1 is dropped, so circuits 2–4 would collide).
7. **`_sub3` / `_sub4` are min / max.** Exposing them as `_min` / `_max`
   (or as the setting's range) would make them usable without knowing the
   suffix rule.

# Registers

All registers of the Estyma controller identified so far. The controller reports
them as `reg,idx,value` in `ZAPISZ_ODCZYT`; idx 1 is the value, idx 3 and 4 hold
the min and max of settings. Any register can be read by adding `reg,idx` to the
`ODCZYT` list in the reply to `/device/connect`; the controller reports it on its
next request.

Encoding:

- **x1**: the raw value as is.
- **÷10 / ÷100**: divide the raw value.
- **8-bit**: reported as `0x100 + value`, use the low byte.
- **16-bit**: use the low 16 bits (reads sometimes carry garbage in the upper bits).
- **signed 16-bit**: low 16 bits as a signed number.

Status: confirmed unless marked *(likely)* or *(guess)*. How each was found:
[register-mapping.md](register-mapping.md) (cloud comparison) and the cloud
service panel, whose rows name their register.

## Live values

| Register | Function | Encoding | Range / values |
|---|---|---|---|
| 8193 | Boiler temperature | ÷10 | °C |
| 8196 | Boiler target, calculated (resulting) | x1 | °C |
| 8199 | Burner state | 8-bit | 0 off, 1 ignition, 2 glowing, 5 modulation, 7 cleaning, 10 error |
| 8466 | Buffer temperature, top | ÷10 | °C |
| 8467 | Buffer temperature, bottom | ÷10 | °C |
| 8473 | Flue gas temperature | 8-bit | °C |
| 8480 | Oxygen in the flue gas | ÷100 | 0..20.95 % |
| 8484 | Fuel counter (total) | ÷10 | kg |
| 8543 | Outside temperature | ÷10 | °C |
| 8823 | Boiler pump | 8-bit | 0 off, 1 on |
| 8857 | Heating circuit 1 pump | 8-bit | 0 off, 1 on |
| 16385 | Heating circuit 1 flow temperature | ÷10 | °C |
| 16389 | Burner power | 8-bit | 0..100 % |
| 16391 | Lambda PWM | 8-bit | 0..100 |

## User settings (main cloud panel)

| Register | Function | Encoding | Range / values |
|---|---|---|---|
| 8197 | Controller on/off | 8-bit | 0 off, 1 on |
| 8194 | Boiler mode | x1 | 0 time automatic, 1 time manual, 2 automatic, 3 manual |
| 8195 | Boiler target | x1 | 60..80 °C |
| 8468 | Buffer target, top | x1 | 7..80 °C |
| 8469 | Buffer target, bottom | x1 | 7..80 °C |
| 8242 | Room comfort temperature, circuit 1 | ÷10 | raw 150..300 = 15.0..30.0 °C |
| 8264 | Room eco temperature, circuit 1 | ÷10 | raw 100..300 = 10.0..30.0 °C |
| 8292 | Heating circuit 1 program *(likely)* | 8-bit | 0 off, 1 economical, 2 temporary, 3 comfortable |
| 8486..8510 step 4 | Circuit 1 weekly timers, Monday..Sunday *(likely; weekday order not proven)* | bit mask | one bit per hour (guess) |

## Service settings: boiler

| Register | Function | Encoding | Range / values | Panel name (Polish) |
|---|---|---|---|---|
| 8712 | Switch-on hysteresis: burner restarts when the boiler falls this far below target | 16-bit | 0..20 °C | Histereza załączenia |
| 8849 | Switch-off hysteresis: burner stops when the boiler rises this far above target | 16-bit | 0..20 °C | Histereza wyłączenia |
| 8711 | Minimum boiler temperature before the pumps may run | 16-bit | 20..70 °C | Minimalna temp. pomp |
| 14088 | Pump overrun after the burner stops | 8-bit | 0..120 min | Wybieg pompy |
| 8556 | Minimum return temperature (return protection) | 16-bit | 35..75 °C | Minimalna temp. powrotu |

## Service settings: burner

| Register | Function | Encoding | Range / values | Panel name (Polish) |
|---|---|---|---|---|
| 8819 | Air correction, added to the blower power | signed 16-bit | -10..40 % (-10 reads as 65526) | Korekta powietrza |
| 8816 | Minimum burner power during modulation | 8-bit | 30..100 % | Moc min |
| 8817 | Maximum burner power during modulation | 8-bit | 30..100 % | Moc max |
| 8801 | Blower power at 20 % burner power | 8-bit | 0..100 % | Moc dmuchawy modulacja 20% |
| 8802 | Blower power at 40 % burner power | 8-bit | 0..100 % | Moc dmuchawy modulacja 40% |
| 8803 | Blower power at 60 % burner power | 8-bit | 0..100 % | Moc dmuchawy modulacja 60% |
| 8804 | Blower power at 80 % burner power | 8-bit | 0..100 % | Moc dmuchawy modulacja 80% |
| 8805 | Blower power at 100 % burner power | 8-bit | 0..100 % | Moc dmuchawy modulacja 100% |
| 8841 | Blower power during ignition | 8-bit | 5..100 % | Moc dmuchawy rozpalanie |
| 8790 | Initial fuel dose before ignition | 16-bit | 0..200 s | Wstępna dawka paliwa |
| 14084 | Blower power during glowing | 8-bit | 0..100 % | Moc dmuchawy rozżarzanie |
| 12300 | Blower power during burn-out | 8-bit | 1..100 % | Moc dmuchawy wygaszanie |
| 8848 | Glowing phase duration | 8-bit | 0..20 min | Czas rozżarzania |
| 8811 | Burn-out phase duration | 8-bit | 0..5 min | Czas wygaszania |
| 10650 | Fuel feed during glowing | 8-bit | 0..100 % | Rozżarzanie paliwo |
| 8818 | Hopper feeder output from the feed test (used for fuel consumption) | 16-bit, ÷10 | raw 50..300 = 5.0..30.0 kg/h | Ilość paliwa test |
| 8850 | Fuel calorific value | 8-bit, ÷10 | raw 20..100 = 2.0..10.0 kWh/kg | Wartość opałowa |
| 13829 | Lambda (O2) control | 8-bit | 0 = off, 1 = on | Sterowanie Lambda |
| 8526 | Target O2 in flue gas at 30 % power | 8-bit, ÷10 | raw 50..150 = 5.0..15.0 % | Tlen dla mocy 30% |
| 8530 | Target O2 in flue gas at 100 % power | 8-bit, ÷10 | raw 50..150 = 5.0..15.0 % | Tlen dla mocy 100% |
| 14092 | Minimum ignition time | 8-bit | 2..6 min | Minimalny czas rozpalania |
| 14091 | Burner feeder run time as % of the hopper feeder | 16-bit | 50..300 % | Pod. Palnika [% pod. zasobnika] |
| 14152 | Burner feeder initial dose (no answer from this controller) | 8-bit | unknown | PP – dawka wstępna |
| 14080 | Hopper feeder cycle period | 8-bit | 5..180 s | Okres podajnika zasobnika |
| 14101 | Pause time (low-load pause mode) | 8-bit | 0..60 min | Czas pauzy |
| 14102 | Burner power in pause mode | 8-bit | 5..30 % | Moc w pauzie |
| 14103 | Blower power in pause mode | 8-bit | 0..50 % | Dmuchawa w pauzie |
| 8791 | Maximum feed time (may be adapted by the controller) | 16-bit | 10..150 s | Podawanie max |

## Service settings: heating circuits

| Register | Function | Encoding | Range / values | Panel name (Polish) |
|---|---|---|---|---|
| 8901 | Circuit 1 weather curve: flow target at -20 °C outside | 8-bit | 20..80 °C | Temperatura CO dla -20°C |
| 8902 | Circuit 2 weather curve: flow target at -20 °C outside | 8-bit | 20..80 °C | Temperatura CO dla -20°C |
| 16396 | Circuit 1 weather curve: flow target at -10 °C outside (no answer from this controller) | 8-bit | unknown | Temperatura CO dla -10°C |
| 16397 | Circuit 2 weather curve: flow target at -10 °C outside (no answer from this controller) | 8-bit | unknown | Temperatura CO dla -10°C |
| 8905 | Circuit 1 weather curve: flow target at 0 °C outside | 8-bit | 20..80 °C | Temperatura CO dla 0°C |
| 8906 | Circuit 2 weather curve: flow target at 0 °C outside | 8-bit | 20..80 °C | Temperatura CO dla 0°C |
| 8909 | Circuit 1 weather curve: flow target at 10 °C outside | 8-bit | 20..80 °C | Temperatura CO dla 10°C |
| 8910 | Circuit 2 weather curve: flow target at 10 °C outside | 8-bit | 20..80 °C | Temperatura CO dla 10°C |
| 8514 | Circuit 1 mode | 8-bit | 0 = manual, 1 = weather-compensated | Tryb pracy |
| 8515 | Circuit 2 mode | 8-bit | 0 = manual, 1 = weather-compensated | Tryb pracy |
| 8518 | Circuit 1 flow target in manual mode | 8-bit | 20..80 °C | Temperatura CO zadana ręcznie |
| 8519 | Circuit 2 flow target in manual mode | 8-bit | 20..80 °C | Temperatura CO zadana ręcznie |

## Alarms

| Register | Function | Encoding | Range / values |
|---|---|---|---|
| 16460 | Alarm counter, +1 on every new alarm | 8-bit | count |
| 16400+k (k = 0..19) | Alarm slot k: state in bits 16+, alarm code in the low byte | x1 | state 3 active, 7 being reset, 0 cleared; code 139 = start failure, 140 = missing fuel / no fire |
| 16420+k | Alarm slot k start time | Unix time | controller clock |
| 16440+k | Alarm slot k acknowledge time ("Bestätigung" on the display) | Unix time | 0 until acknowledged |

The slot in use follows the counter. Timestamps run about 45 minutes behind real time.

## System

| Register | Function | Encoding | Range / values |
|---|---|---|---|
| 4112 | Apply flag, the server sends 1 with every write | x1 | 1 |
| 8549 | Controller clock, written by the server | Unix time | local time |
| 8485 | Device type *(likely)* | x1 | e.g. 1000040002 |
| 8198, 8453..8456, 8483, 8548, 8720, 8865 | Configuration flags (lambda fitted, number of buffers / circuits, ...) *(guess, not told apart)* | 8-bit | 0 / 1 |

Not found yet: rated power, minimum blower speed, blower air speed F(30 %) / F(100 %).

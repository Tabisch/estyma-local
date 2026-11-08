# Service settings

Settings from the cloud service panel (Panel serwisowy), identified by register.
All are read at index 1 (value); index 3 and 4 hold the min and max, which match the ranges below.
8-bit settings are reported as `0x100 + value` (the panel reads them with mask 255), so take the low byte.
Values are stored raw; where a range says ÷10, divide the raw value by 10.
For heating circuits the first register is CO1, the second CO2.

Reading: add `reg,idx` to the ODCZYT list in the reply to `/device/connect`; the controller reports it on its next request.

## Boiler (Kocioł)

| Register | Panel name (Polish) | Function | Range |
|---|---|---|---|
| 8712 | Histereza załączenia | Switch-on hysteresis: how far the boiler temperature may fall below the target before the burner starts again | 0..20 °C |
| 8849 | Histereza wyłączenia | Switch-off hysteresis: how far the boiler temperature may rise above the target before the burner stops | 0..20 °C |
| 8711 | Minimalna temp. pomp | Minimum boiler temperature before the circulation pumps may run | 20..70 °C |
| 14088 | Wybieg pompy | Pump overrun after the burner stops | 0..120 min |
| 8556 | Minimalna temp. powrotu | Minimum return temperature (return protection) | 35..75 °C |

## Burner (Palnik)

| Register | Panel name (Polish) | Function | Range |
|---|---|---|---|
| 8819 | Korekta powietrza | Air correction, added to the blower power | -10..40 % (signed 16-bit, -10 reads as 65526) |
| 8816 | Moc min | Minimum burner power during modulation | 30..100 % |
| 8817 | Moc max | Maximum burner power during modulation | 30..100 % |
| 8801 | Moc dmuchawy modulacja 20% | Blower power at 20 % burner power | 0..100 % |
| 8802 | Moc dmuchawy modulacja 40% | Blower power at 40 % burner power | 0..100 % |
| 8803 | Moc dmuchawy modulacja 60% | Blower power at 60 % burner power | 0..100 % |
| 8804 | Moc dmuchawy modulacja 80% | Blower power at 80 % burner power | 0..100 % |
| 8805 | Moc dmuchawy modulacja 100% | Blower power at 100 % burner power | 0..100 % |
| 8841 | Moc dmuchawy rozpalanie | Blower power during ignition | 5..100 % |
| 8790 | Wstępna dawka paliwa | Initial fuel dose fed before ignition | 0..200 s |
| 14084 | Moc dmuchawy rozżarzanie | Blower power during glowing (after ignition) | 0..100 % |
| 12300 | Moc dmuchawy wygaszanie | Blower power during burn-out | 1..100 % |
| 8848 | Czas rozżarzania | Glowing phase duration | 0..20 min |
| 8811 | Czas wygaszania | Burn-out phase duration | 0..5 min |
| 10650 | Rozżarzanie paliwo | Fuel feed during glowing | 0..100 % |
| 8818 | Ilość paliwa test | Hopper feeder output measured in the feed test (used for fuel consumption) | raw 50..300 = 5.0..30.0 kg/h (÷10) |
| 8850 | Wartość opałowa | Fuel calorific value | raw 20..100 = 2.0..10.0 kWh/kg (÷10) |
| 13829 | Sterowanie Lambda | Lambda (O2) control | 0 = off, 1 = on |
| 8526 | Tlen dla mocy 30% | Target O2 in the flue gas at 30 % power | raw 50..150 = 5.0..15.0 % (÷10) |
| 8530 | Tlen dla mocy 100% | Target O2 in the flue gas at 100 % power | raw 50..150 = 5.0..15.0 % (÷10) |
| 14092 | Minimalny czas rozpalania | Minimum ignition time | 2..6 min |
| 14091 | Pod. Palnika [% pod. zasobnika] | Burner feeder run time as % of the hopper feeder | 50..300 % |
| 14152 | PP – dawka wstępna | Initial dose of the burner feeder (no answer from this controller) | unknown |
| 14080 | Okres podajnika zasobnika | Hopper feeder cycle period | 5..180 s |
| 14101 | Czas pauzy | Pause time (low-load pause mode) | 0..60 min |
| 14102 | Moc w pauzie | Burner power in pause mode | 5..30 % |
| 14103 | Dmuchawa w pauzie | Blower power in pause mode | 0..50 % |
| 8791 | Podawanie max | Maximum feed time | 10..150 s |

## Heating circuits (CO1 / CO2)

| Register | Panel name (Polish) | Function | Range |
|---|---|---|---|
| 8901 / 8902 | Temperatura CO dla -20°C | Weather curve: flow target at -20 °C outside | 20..80 °C |
| 16396 / 16397 | Temperatura CO dla -10°C | Weather curve: flow target at -10 °C outside (no answer from this controller) | unknown |
| 8905 / 8906 | Temperatura CO dla 0°C | Weather curve: flow target at 0 °C outside | 20..80 °C |
| 8909 / 8910 | Temperatura CO dla 10°C | Weather curve: flow target at 10 °C outside | 20..80 °C |
| 8514 / 8515 | Tryb pracy | Circuit mode | 0 = manual (Ręczny), 1 = weather (Pogodowy) |
| 8518 / 8519 | Temperatura CO zadana ręcznie | Flow target in manual mode | 20..80 °C |

Not in the service panel and not yet found: rated power, minimum blower speed, blower air speed F(30 %) / F(100 %).

Note: 8791 (max feed) changed from 107 s to 143 s without a manual edit on one controller, so the controller may adapt it itself.

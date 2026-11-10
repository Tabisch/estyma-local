"""Everything known about the controller's registers, in one place.

This is the single source for register labels, decoding, units, setting
ranges, enum value names, alarm texts and the documentation in
``docs/registers.md``. The rest of the code reads it from here:

- ``registers.py``: the raw register table on ``/registers``,
- ``monitor.py``: the decoded values on the monitor page (and MQTT),
- ``controller.py``: the settings on ``/heating`` and their write limits,
- ``tools/gendocs.py``: generates ``docs/registers.md`` from it
  (``tests/test_docs.py`` fails when the doc is out of date).

To add a register, add a ``Reg`` to ``REGS`` and run
``python3 tools/gendocs.py``.

decode: "x1" as is, "d10" / "d100" divided, "u8" low byte (8-bit registers
report 0x100 + value), "u16" low 16 bits, "s16" signed low 16 bits,
"u8/10" and "u16/10" masked then divided, "time" Unix time (controller
clock), "hex" shown as hex (bit masks), "alarm" alarm slot (state << 16 | code).

plausible / range: a decoded value outside ``plausible`` (or, without it,
outside the setting ``range``) is treated as a corrupted CAN read, see
``decode.py``.

conf: "confirmed", "likely" or "guess". Index 1 of a register is its value,
3 and 4 its min and max (settings only).
"""

from __future__ import annotations

from dataclasses import dataclass

# Sections of docs/registers.md (and of nothing else), in order.
SECTIONS = {
    "live": "Live values",
    "user": "User settings (main cloud panel)",
    "boiler": "Service settings: boiler",
    "burner": "Service settings: burner",
    "circuits": "Service settings: heating circuits",
    "alarms": "Alarms",
    "system": "System",
}
# Sections that come from the cloud's service panel (they have a Polish name).
SERVICE_SECTIONS = ("boiler", "burner", "circuits")


@dataclass(frozen=True)
class Reg:
    reg: int
    label: str                  # English, shown in the web UI
    label_de: str               # German, shown in the web UI
    decode: str
    unit: str = ""
    section: str = "system"
    conf: str = "confirmed"
    range: tuple | None = None  # setting range in shown units (cloud panel)
    values: dict | None = None  # enum: value -> (en, de)
    panel: str = ""             # name on the cloud's (Polish) service panel
    doc: str = ""               # longer function text for the docs (default: label)
    note: str = ""              # "Range / values" text for the docs (default: generated)
    plausible: tuple | None = None  # live values: decoded values outside are corrupted reads

    @property
    def scale(self) -> int:
        """Shown value = raw / scale."""
        return 10 if self.decode in ("d10", "u8/10", "u16/10") else 100 if self.decode == "d100" else 1


_ONOFF = {0: ("off", "aus"), 1: ("on", "an")}
_CIRCUIT_MODE = {0: ("manual", "manuell"), 1: ("weather", "witterungsgeführt")}
_NO_ANSWER = " (no answer from this controller)"

REGS = [
    # -- live values --------------------------------------------------------
    Reg(8193, "Boiler temperature", "Kesseltemperatur", "d10", "°C", "live", plausible=(-20, 120)),
    Reg(8196, "Boiler target (calculated)", "Kessel-Soll (berechnet)", "x1", "°C", "live",
        doc="Boiler target, calculated (resulting)", plausible=(0, 120)),
    Reg(8199, "Burner state", "Brennerstatus", "u8", "", "live",
        values={0: ("off", "aus"), 1: ("ignition", "Zünden"), 2: ("glowing", "Glühen"),
                5: ("modulation", "Modulation"), 7: ("cleaning", "Reinigung"), 10: ("error", "Fehler")}, plausible=(0, 30)),
    Reg(8466, "Buffer top", "Puffer oben", "d10", "°C", "live", doc="Buffer temperature, top", plausible=(-20, 120)),
    Reg(8467, "Buffer bottom", "Puffer unten", "d10", "°C", "live", doc="Buffer temperature, bottom", plausible=(-20, 120)),
    Reg(8473, "Flue gas temperature", "Abgastemperatur", "u8", "°C", "live", plausible=(0, 255)),
    Reg(8480, "Oxygen in flue gas", "Sauerstoff im Abgas", "d100", "%", "live",
        doc="Oxygen in the flue gas", note="0..20.95 %", plausible=(0, 25)),
    Reg(8484, "Fuel counter", "Brennstoffzähler", "d10", "kg", "live", doc="Fuel counter (total)", plausible=(0, 2**31 - 1)),
    Reg(8543, "Outside temperature", "Außentemperatur", "d10", "°C", "live", plausible=(-50, 60)),
    Reg(8823, "Boiler pump", "Kesselpumpe", "u8", "", "live", values=_ONOFF, plausible=(0, 1)),
    Reg(8857, "Heating circuit 1 pump", "Pumpe Heizkreis 1", "u8", "", "live", values=_ONOFF, plausible=(0, 1)),
    Reg(16385, "Heating circuit 1 temperature", "Heizkreis 1 Temperatur", "d10", "°C", "live",
        doc="Heating circuit 1 flow temperature", plausible=(-20, 120)),
    Reg(16389, "Burner power", "Brennerleistung", "u8", "%", "live", note="0..100 %", plausible=(0, 100)),
    Reg(16391, "Lambda PWM", "Lambda-PWM", "u8", "", "live", note="0..100", plausible=(0, 255)),

    # -- user settings (main cloud panel) -----------------------------------
    Reg(8197, "Controller on/off", "Regler an/aus", "u8", "", "user", values=_ONOFF, plausible=(0, 1)),
    Reg(8194, "Boiler mode", "Kesselmodus", "x1", "", "user",
        values={0: ("time automatic", "Zeitautomatik"), 1: ("time manual", "Zeit manuell"),
                2: ("automatic", "Automatik"), 3: ("manual", "Manuell")}, plausible=(0, 255)),
    Reg(8195, "Boiler target", "Kessel-Soll", "x1", "°C", "user", range=(60, 80), plausible=(0, 120)),
    Reg(8468, "Buffer top target", "Puffer oben Soll", "x1", "°C", "user", range=(7, 80),
        doc="Buffer target, top", plausible=(0, 120)),
    Reg(8469, "Buffer bottom target", "Puffer unten Soll", "x1", "°C", "user", range=(7, 80),
        doc="Buffer target, bottom", plausible=(0, 120)),
    Reg(8242, "Room comfort temperature, circuit 1", "Raum Komfort, Heizkreis 1", "d10", "°C", "user",
        range=(15.0, 30.0), plausible=(0, 40)),
    Reg(8264, "Room eco temperature, circuit 1", "Raum Eco, Heizkreis 1", "d10", "°C", "user",
        range=(10.0, 30.0), plausible=(0, 40)),
    Reg(8292, "Heating circuit 1 program", "Programm Heizkreis 1", "u8", "", "user", conf="likely",
        values={0: ("off", "aus"), 1: ("economical", "Eco"), 2: ("temporary", "temporär"),
                3: ("comfortable", "Komfort")}, plausible=(0, 255)),

    # -- service settings: boiler (panel "Kocioł") --------------------------
    Reg(8712, "Switch-on hysteresis (below target)", "Einschalthysterese", "u16", "°C", "boiler",
        range=(0, 20), panel="Histereza załączenia",
        doc="Switch-on hysteresis: burner restarts when the boiler falls this far below target"),
    Reg(8849, "Switch-off hysteresis (above target)", "Ausschalthysterese", "u16", "°C", "boiler",
        range=(0, 20), panel="Histereza wyłączenia",
        doc="Switch-off hysteresis: burner stops when the boiler rises this far above target"),
    Reg(8711, "Minimum boiler temperature before the pumps may run", "Min. Pumpentemperatur", "u16", "°C",
        "boiler", range=(20, 70), panel="Minimalna temp. pomp"),
    Reg(14088, "Pump overrun after the burner stops", "Pumpennachlauf", "u8", "min", "boiler",
        range=(0, 120), panel="Wybieg pompy"),
    Reg(8556, "Minimum return temperature (return protection)", "Min. Rücklauftemperatur", "u16", "°C",
        "boiler", range=(35, 75), panel="Minimalna temp. powrotu"),

    # -- service settings: burner (panel "Palnik") --------------------------
    Reg(8819, "Air correction, added to the blower power", "Luftkorrektur", "s16", "%", "burner",
        range=(-10, 40), panel="Korekta powietrza", note="-10..40 % (-10 reads as 65526)"),
    Reg(8816, "Minimum burner power during modulation", "Min. Leistung", "u8", "%", "burner",
        range=(30, 100), panel="Moc min"),
    Reg(8817, "Maximum burner power during modulation", "Max. Leistung", "u8", "%", "burner",
        range=(30, 100), panel="Moc max"),
    *[Reg(8800 + i, f"Blower power at {p} % burner power", f"Gebläse bei {p} % Leistung", "u8", "%",
          "burner", range=(0, 100), panel=f"Moc dmuchawy modulacja {p}%")
      for i, p in enumerate((20, 40, 60, 80, 100), 1)],
    Reg(8841, "Blower power during ignition", "Gebläse Anzünden", "u8", "%", "burner",
        range=(5, 100), panel="Moc dmuchawy rozpalanie"),
    Reg(8790, "Initial fuel dose", "Anfangsdosis", "u16", "s", "burner", range=(0, 200),
        panel="Wstępna dawka paliwa", doc="Initial fuel dose before ignition"),
    Reg(14084, "Blower power during glowing", "Gebläse Vorglühen", "u8", "%", "burner",
        range=(0, 100), panel="Moc dmuchawy rozżarzanie"),
    Reg(12300, "Blower power during burn-out", "Gebläse Ausbrand", "u8", "%", "burner",
        range=(1, 100), panel="Moc dmuchawy wygaszanie"),
    Reg(8848, "Glowing phase duration", "Glühzeit", "u8", "min", "burner", range=(0, 20),
        panel="Czas rozżarzania"),
    Reg(8811, "Burn-out phase duration", "Abbrennzeit", "u8", "min", "burner", range=(0, 5),
        panel="Czas wygaszania"),
    Reg(10650, "Fuel feed during glowing", "Brennstoff beim Vorglühen", "u8", "%", "burner",
        range=(0, 100), panel="Rozżarzanie paliwo"),
    Reg(8818, "Hopper feeder output from the feed test (used for fuel consumption)",
        "Förderleistung Behälterschnecke", "u16/10", "kg/h", "burner", range=(5.0, 30.0),
        panel="Ilość paliwa test"),
    Reg(8850, "Fuel calorific value", "Brennstoffbrennwert", "u8/10", "kWh/kg", "burner",
        range=(2.0, 10.0), panel="Wartość opałowa"),
    Reg(13829, "Lambda (O2) control", "Lambdasteuerung", "u8", "", "burner", values=_ONOFF,
        panel="Sterowanie Lambda"),
    Reg(8526, "Target O2 in flue gas at 30 % power", "O2 bei 30 % Leistung", "u8/10", "%", "burner",
        range=(5.0, 15.0), panel="Tlen dla mocy 30%"),
    Reg(8530, "Target O2 in flue gas at 100 % power", "O2 bei 100 % Leistung", "u8/10", "%", "burner",
        range=(5.0, 15.0), panel="Tlen dla mocy 100%"),
    Reg(14092, "Minimum ignition time", "Min. Anzündzeit", "u8", "min", "burner", range=(2, 6),
        panel="Minimalny czas rozpalania"),
    Reg(14091, "Burner feeder run time as % of the hopper feeder", "Brennerschnecke in % der Behälterschnecke",
        "u16", "%", "burner", range=(50, 300), panel="Pod. Palnika [% pod. zasobnika]"),
    Reg(14152, "Burner feeder initial dose" + _NO_ANSWER, "Brennerschnecke Anfangsdosis", "u8", "s",
        "burner", panel="PP – dawka wstępna", note="unknown"),
    Reg(14080, "Hopper feeder cycle period", "Schnecke Intervall", "u8", "s", "burner", range=(5, 180),
        panel="Okres podajnika zasobnika"),
    Reg(14101, "Pause time (low-load pause mode)", "Pausenzeit", "u8", "min", "burner", range=(0, 60),
        panel="Czas pauzy"),
    Reg(14102, "Burner power in pause mode", "Pausenleistung", "u8", "%", "burner", range=(5, 30),
        panel="Moc w pauzie"),
    Reg(14103, "Blower power in pause mode", "Pausengebläse", "u8", "%", "burner", range=(0, 50),
        panel="Dmuchawa w pauzie"),
    Reg(8791, "Maximum feed time (may be adapted by the controller)", "Max. Förderzeit", "u16", "s",
        "burner", range=(10, 150), panel="Podawanie max"),
]

# -- service settings: heating circuits (panel "CO1" / "CO2") ---------------
# Circuit 2 registers are circuit 1 + 1.
for _c in (1, 2):
    _o = _c - 1
    REGS += [
        Reg(8901 + _o, f"Circuit {_c} weather curve: flow target at -20 °C outside",
            f"Heizkreis {_c} Heizkurve: Vorlauf bei -20 °C außen", "u8", "°C", "circuits",
            range=(20, 80), panel="Temperatura CO dla -20°C"),
        Reg(16396 + _o, f"Circuit {_c} weather curve: flow target at -10 °C outside" + _NO_ANSWER,
            f"Heizkreis {_c} Heizkurve: Vorlauf bei -10 °C außen", "u8", "°C", "circuits",
            panel="Temperatura CO dla -10°C", note="unknown"),
        Reg(8905 + _o, f"Circuit {_c} weather curve: flow target at 0 °C outside",
            f"Heizkreis {_c} Heizkurve: Vorlauf bei 0 °C außen", "u8", "°C", "circuits",
            range=(20, 80), panel="Temperatura CO dla 0°C"),
        Reg(8909 + _o, f"Circuit {_c} weather curve: flow target at 10 °C outside",
            f"Heizkreis {_c} Heizkurve: Vorlauf bei 10 °C außen", "u8", "°C", "circuits",
            range=(20, 80), panel="Temperatura CO dla 10°C"),
        Reg(8514 + _o, f"Circuit {_c} mode", f"Heizkreis {_c} Modus", "u8", "", "circuits",
            values=_CIRCUIT_MODE, panel="Tryb pracy", note="0 manual, 1 weather-compensated"),
        Reg(8518 + _o, f"Circuit {_c} flow target in manual mode", f"Heizkreis {_c} Vorlauf-Soll manuell",
            "u8", "°C", "circuits", range=(20, 80), panel="Temperatura CO zadana ręcznie"),
    ]

# -- alarms -----------------------------------------------------------------
# Ring buffer of the last 20 alarms: slot k uses 16400+k (state << 16 | code),
# 16420+k start, 16440+k acknowledge time (0 while not acknowledged).
# 16460 (0x100 + n) counts the alarms; the newest slot is (n - 1) % 20.
ALARM_SLOTS = 20
ALARM_CODE, ALARM_START, ALARM_END, ALARM_COUNTER = 16400, 16420, 16440, 16460
ALARM_STATES = {0: "cleared", 3: "active", 7: "resetting"}
# Alarm code -> text, as the controller display shows it (German) in English.
# Add new codes as they show up; unknown ones are shown as "Code n".
ALARM_TEXT = {
    2: "boiler overheating",                  # "Überhitzung des Kessels"
    139: "start failure",                     # "Fehler beim Starten"
    140: "missing fuel / no fire",            # "Brennstoffmangel / Feuermangel"
    201: "burner fan damaged",                # "Beschädigung des Brennergebläses"
    203: "oxygen level very low",
}

REGS.append(Reg(ALARM_COUNTER, "Alarm counter", "Alarmzähler", "u8", "", "alarms",
                doc="Alarm counter, +1 on every new alarm", note="count"))
for _k in range(ALARM_SLOTS):
    REGS += [
        Reg(ALARM_CODE + _k, f"Alarm slot {_k}: state and code", f"Alarm-Platz {_k}: Zustand und Code",
            "alarm", section="alarms"),
        Reg(ALARM_START + _k, f"Alarm slot {_k}: start", f"Alarm-Platz {_k}: Beginn", "time",
            section="alarms"),
        Reg(ALARM_END + _k, f"Alarm slot {_k}: end (0 = active)", f"Alarm-Platz {_k}: Ende (0 = aktiv)",
            "time", section="alarms"),
    ]

# -- system -----------------------------------------------------------------
REGS += [
    Reg(4112, "Apply flag (sent with every write)", "Übernahme-Flag (bei jedem Schreiben)", "x1",
        doc="Apply flag, the server sends 1 with every write", note="1"),
    Reg(8549, "Controller clock (written by the server)", "Regleruhr (vom Server geschrieben)", "time",
        doc="Controller clock, written by the server", note="local time"),
    Reg(8485, "Device type", "Gerätetyp", "x1", conf="likely", note="e.g. 1000040002"),
]
# Probable configuration flags (lambda fitted, number of buffers, number of
# heating circuits, ...): constant 0x100 / 0x101, not yet told apart.
CONFIG_FLAGS = (8198, 8453, 8454, 8455, 8456, 8483, 8548, 8720, 8865)
REGS += [Reg(r, "Configuration flag", "Konfigurations-Flag", "u8", conf="guess") for r in CONFIG_FLAGS]

# Circuit 1 weekly timers, Monday..Sunday: 8486 + 4 * day, one bit per hour.
WEEKLY_TIMERS = tuple(8486 + 4 * _i for _i in range(7))
_DAYS = [("Monday", "Montag"), ("Tuesday", "Dienstag"), ("Wednesday", "Mittwoch"),
         ("Thursday", "Donnerstag"), ("Friday", "Freitag"), ("Saturday", "Samstag"),
         ("Sunday", "Sonntag")]
REGS += [Reg(r, f"Circuit 1 weekly timer, {en}", f"Wochenprogramm Heizkreis 1, {de}", "hex",
             section="user", conf="likely") for r, (en, de) in zip(WEEKLY_TIMERS, _DAYS)]

# Registers that the docs show as one row for a whole group, instead of one
# row each: section -> [(register text, function, encoding, range / values)].
_ALARM_CODES = ", ".join(f"{c} = {t}" for c, t in sorted(ALARM_TEXT.items()))
GROUP_ROWS = {
    "user": [("8486..8510 step 4", "Circuit 1 weekly timers, Monday..Sunday "
              "*(likely; weekday order not proven)*", "bit mask", "one bit per hour (guess)")],
    "alarms": [
        ("16400+k (k = 0..19)", "Alarm slot k: state in bits 16+, alarm code in the low byte", "x1",
         f"state 3 active, 7 being reset, 0 cleared; code {_ALARM_CODES}"),
        ("16420+k", "Alarm slot k start time", "Unix time", "controller clock"),
        ("16440+k", "Alarm slot k acknowledge time (\"Bestätigung\" on the display)", "Unix time",
         "0 until acknowledged"),
    ],
    "system": [("8198, 8453..8456, 8483, 8548, 8720, 8865", "Configuration flags (lambda fitted, "
                "number of buffers / circuits, ...) *(guess, not told apart)*", "8-bit", "0 / 1")],
}
GROUPED = set(WEEKLY_TIMERS) | set(CONFIG_FLAGS) | {
    r for k in range(ALARM_SLOTS) for r in (ALARM_CODE + k, ALARM_START + k, ALARM_END + k)}

BY_REG: dict[int, Reg] = {}
for _r in REGS:
    assert _r.reg not in BY_REG, f"register {_r.reg} defined twice"
    assert _r.section in SECTIONS, f"register {_r.reg}: unknown section {_r.section}"
    BY_REG[_r.reg] = _r

# -- views used by the rest of the code -------------------------------------
# reg: (label, label_de, decode, unit, conf)
CATALOG: dict[int, tuple] = {r.reg: (r.label, r.label_de, r.decode, r.unit, r.conf) for r in REGS}
# Setting ranges (shown units) for registers that have one.
RANGES: dict[int, tuple] = {r.reg: r.range for r in REGS if r.range}
# Names of enumerated values: reg -> {value: (en, de)}.
VALUES: dict[int, dict] = {r.reg: r.values for r in REGS if r.values}
UNKNOWN = ("", "", "x1", "", "unknown")


def get(reg: int) -> Reg:
    return BY_REG[reg]

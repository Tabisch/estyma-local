"""Raw register table: every register seen in the traffic, with its latest value.

Fed by ``Monitor`` with each controller report (``ZAPISZ_ODCZYT``) and the
read list the server answered with (``ODCZYT``). The table only ever grows: a
register that shows up for the first time (for example one the cloud's
service panel added to the read list) gets a row of its own, labelled from
``CATALOG`` or "unknown". The table is saved to ``registers.json`` in the log
directory, so registers that were read only once survive a restart.

Corrupted CAN reads last for one report, so a value only counts as "stable"
once two reports in a row agree. The page shows the stable value and flags a
raw value that differs from it.

The web UI (log_ui.py) serves the page (registers.html) and ``/api/registers``.
"""

from __future__ import annotations

import json
import os
import threading
import time

# --------------------------------------------------------------------------
# What is known about the registers (docs/register-mapping.md and
# docs/service-settings.md). Index 1 is the value, 3 and 4 its
# min and max; the page adds that to the label.
# --------------------------------------------------------------------------
# decode: "x1" as is, "d10" / "d100" divided, "u8" low byte (8-bit registers
# report 0x100 + value), "u16" low 16 bits, "s16" signed low 16 bits,
# "u8/10" and "u16/10" masked then divided, "time" Unix time (controller
# clock), "hex" shown as hex. conf: "confirmed", "likely" or "guess".

# reg: (label, label_de, decode, unit, conf)
CATALOG: dict[int, tuple] = {
    4112: ("Apply flag (sent with every write)", "Übernahme-Flag (bei jedem Schreiben)", "x1", "", "confirmed"),
    8193: ("Boiler temperature", "Kesseltemperatur", "d10", "°C", "confirmed"),
    8194: ("Boiler mode", "Kesselmodus", "x1", "", "confirmed"),
    8195: ("Boiler target", "Kessel-Soll", "x1", "°C", "confirmed"),
    8196: ("Boiler target (calculated)", "Kessel-Soll (berechnet)", "x1", "°C", "confirmed"),
    8197: ("Controller on/off", "Regler an/aus", "u8", "", "confirmed"),
    8199: ("Burner state", "Brennerstatus", "u8", "", "confirmed"),
    8242: ("Room comfort temperature, circuit 1", "Raum Komfort, Heizkreis 1", "d10", "°C", "confirmed"),
    8264: ("Room eco temperature, circuit 1", "Raum Eco, Heizkreis 1", "d10", "°C", "confirmed"),
    8292: ("Heating circuit 1 program", "Programm Heizkreis 1", "u8", "", "likely"),
    8466: ("Buffer top", "Puffer oben", "d10", "°C", "confirmed"),
    8467: ("Buffer bottom", "Puffer unten", "d10", "°C", "confirmed"),
    8468: ("Buffer top target", "Puffer oben Soll", "x1", "°C", "confirmed"),
    8469: ("Buffer bottom target", "Puffer unten Soll", "x1", "°C", "confirmed"),
    8473: ("Flue gas temperature", "Abgastemperatur", "u8", "°C", "confirmed"),
    8480: ("Oxygen in flue gas", "Sauerstoff im Abgas", "d100", "%", "confirmed"),
    8484: ("Fuel counter", "Brennstoffzähler", "d10", "kg", "confirmed"),
    8485: ("Device type", "Gerätetyp", "x1", "", "likely"),
    8543: ("Outside temperature", "Außentemperatur", "d10", "°C", "confirmed"),
    8549: ("Controller clock (written by the server)", "Regleruhr (vom Server geschrieben)", "time", "", "confirmed"),
    8823: ("Boiler pump", "Kesselpumpe", "u8", "", "confirmed"),
    8857: ("Heating circuit 1 pump", "Pumpe Heizkreis 1", "u8", "", "confirmed"),
    16385: ("Heating circuit 1 temperature", "Heizkreis 1 Temperatur", "d10", "°C", "confirmed"),
    16389: ("Burner power", "Brennerleistung", "u8", "%", "confirmed"),
    16391: ("Lambda PWM", "Lambda-PWM", "u8", "", "confirmed"),
    16460: ("Alarm counter", "Alarmzähler", "u8", "", "confirmed"),
    # Service settings, identified from the cloud service panel (the row ids
    # are the registers). 16-bit ones were read with mask 65535, the rest 255.
    # Boiler
    8712: ("Boiler switch-on hysteresis", "Kessel Einschalthysterese", "u16", "°C", "confirmed"),
    8849: ("Boiler switch-off hysteresis", "Kessel Ausschalthysterese", "u16", "°C", "confirmed"),
    8711: ("Minimum pump temperature", "Pumpen-Mindesttemperatur", "u16", "°C", "confirmed"),
    14088: ("Pump overrun", "Pumpennachlauf", "u8", "min", "confirmed"),
    8556: ("Minimum return temperature", "Min. Rücklauftemperatur", "u16", "°C", "confirmed"),
    # Burner
    8819: ("Air correction", "Luftkorrektur", "s16", "%", "confirmed"),
    8816: ("Minimum burner power", "Min. Leistung", "u8", "%", "confirmed"),
    8817: ("Maximum burner power", "Max. Leistung", "u8", "%", "confirmed"),
    8841: ("Blower power during ignition", "Gebläse Anzünden", "u8", "%", "confirmed"),
    8790: ("Initial fuel dose", "Anfangsdosis", "u16", "s", "confirmed"),
    14084: ("Blower power during glowing", "Gebläse Vorglühen", "u8", "%", "confirmed"),
    12300: ("Blower power during burn-out", "Gebläse Ausbrand", "u8", "%", "confirmed"),
    8848: ("Glow time", "Glühzeit", "u8", "min", "confirmed"),
    8811: ("Burn-out time", "Abbrennzeit", "u8", "min", "confirmed"),
    10650: ("Fuel during glowing", "Vorglühen (Brennstoff)", "u8", "%", "confirmed"),
    8818: ("Hopper auger feed rate (test)", "Förderleistung Behälterschnecke", "u16/10", "kg/h", "confirmed"),
    8850: ("Fuel calorific value", "Brennstoffbrennwert", "u8/10", "kWh/kg", "confirmed"),
    13829: ("Lambda control", "Lambdamodus", "u8", "", "confirmed"),
    8526: ("O2 target at 30 % power", "O2 bei 30 % Leistung", "u8/10", "%", "confirmed"),
    8530: ("O2 target at 100 % power", "O2 bei 100 % Leistung", "u8/10", "%", "confirmed"),
    14092: ("Minimum ignition time", "Min. Anzündzeit", "u8", "min", "confirmed"),
    14091: ("Burner auger (% of hopper auger)", "Brennerschnecke (% der Behälterschnecke)", "u16", "%", "confirmed"),
    14152: ("Burner auger initial dose", "Brennerschnecke Anfangsdosis", "u8", "", "confirmed"),
    14080: ("Hopper auger period", "Behälterschnecke Intervall", "u8", "s", "confirmed"),
    14101: ("Pause time", "Pausenzeit", "u8", "min", "confirmed"),
    14102: ("Pause power", "Pausenleistung", "u8", "%", "confirmed"),
    14103: ("Pause blower", "Pausengebläse", "u8", "%", "confirmed"),
    8791: ("Maximum feed time", "Max. Förderzeit", "u16", "s", "confirmed"),
}

# Probable configuration flags (lambda fitted, number of buffers, number of
# heating circuits, ...): constant 0x100 / 0x101, not yet told apart.
for _r in (8198, 8453, 8454, 8455, 8456, 8483, 8548, 8720, 8865):
    CATALOG[_r] = ("Configuration flag", "Konfigurations-Flag", "u8", "", "guess")

_DAYS = [("Monday", "Montag"), ("Tuesday", "Dienstag"), ("Wednesday", "Mittwoch"),
         ("Thursday", "Donnerstag"), ("Friday", "Freitag"), ("Saturday", "Samstag"),
         ("Sunday", "Sonntag")]
for _i, (_en, _de) in enumerate(_DAYS):
    CATALOG[8486 + 4 * _i] = (f"Circuit 1 weekly timer, {_en}", f"Wochenprogramm Heizkreis 1, {_de}",
                              "hex", "", "likely")
for _i in range(5):
    CATALOG[8801 + _i] = (f"Blower power at {20 * (_i + 1)} % modulation",
                          f"Gebläseleistung bei {20 * (_i + 1)} % Modulation", "u8", "%", "confirmed")
# Heating circuits 1 and 2: weather curve, mode and manual target.
for _c in (1, 2):
    for _base, _out in ((8901, -20), (16396, -10), (8905, 0), (8909, 10)):
        CATALOG[_base + _c - 1] = (f"Circuit {_c} flow temperature at {_out} °C outside",
                                   f"Heizkreis {_c} Vorlauf bei {_out} °C außen", "u8", "°C", "confirmed")
    CATALOG[8513 + _c] = (f"Circuit {_c} mode", f"Betriebsart Heizkreis {_c}", "u8", "", "confirmed")
    CATALOG[8517 + _c] = (f"Circuit {_c} manual flow target", f"Heizkreis {_c} Vorlauf-Soll manuell",
                          "u8", "°C", "confirmed")

# Setting ranges from the service panel (decoded units), for registers that
# do not report their own min/max as index 3/4.
RANGES: dict[int, tuple] = {
    8712: (0, 20), 8849: (0, 20), 8711: (20, 70), 14088: (0, 120), 8556: (35, 75),
    8819: (-10, 40), 8816: (30, 100), 8817: (30, 100), 8841: (5, 100), 8790: (0, 200),
    14084: (0, 100), 12300: (1, 100), 8848: (0, 20), 8811: (0, 5), 10650: (0, 100),
    8818: (5.0, 30.0), 8850: (2.0, 10.0), 8526: (5.0, 15.0), 8530: (5.0, 15.0),
    14092: (2, 6), 14091: (50, 300), 14080: (5, 180), 14101: (0, 60), 14102: (5, 30),
    14103: (0, 50), 8791: (10, 150),
    **{r: (0, 100) for r in range(8801, 8806)},
    **{r: (20, 80) for r in (8901, 8902, 8905, 8906, 8909, 8910, 8518, 8519)},
}

# Names of enumerated values: reg -> {value: (en, de)}.
_ONOFF = {0: ("off", "aus"), 1: ("on", "an")}
VALUES: dict[int, dict] = {
    8194: {0: ("time automatic", "Zeitautomatik"), 1: ("time manual", "Zeit manuell"),
           2: ("automatic", "Automatik"), 3: ("manual", "Manuell")},
    8197: _ONOFF, 8823: _ONOFF, 8857: _ONOFF,
    8199: {0: ("off", "aus"), 1: ("ignition", "Zünden"), 2: ("glowing", "Glühen"),
           5: ("modulation", "Modulation"), 7: ("cleaning", "Reinigung"), 10: ("error", "Fehler")},
    8292: {0: ("off", "aus"), 1: ("economical", "Eco"), 2: ("temporary", "temporär"),
           3: ("comfortable", "Komfort")},
    13829: _ONOFF,
    **{r: {0: ("manual", "manuell"), 1: ("weather", "witterungsgeführt")} for r in (8514, 8515)},
}

# Alarm ring buffer: slot k uses 16400+k (state << 16 | code), 16420+k start,
# 16440+k end (0 while active).
for _k in range(20):
    CATALOG[16400 + _k] = (f"Alarm slot {_k}: state and code", f"Alarm-Platz {_k}: Zustand und Code",
                           "alarm", "", "confirmed")
    CATALOG[16420 + _k] = (f"Alarm slot {_k}: start", f"Alarm-Platz {_k}: Beginn", "time", "", "confirmed")
    CATALOG[16440 + _k] = (f"Alarm slot {_k}: end (0 = active)", f"Alarm-Platz {_k}: Ende (0 = aktiv)",
                           "time", "", "confirmed")

UNKNOWN = ("", "", "x1", "", "unknown")


def decode(raw: int, how: str):
    """Raw report value -> number or text for the page (None if not decodable)."""
    if raw is None:
        return None
    scale = 1
    if how.endswith("/10"):
        how, scale = how[:-3], 10
    if how == "u8":
        val = raw & 0xFF
    elif how == "u16":
        val = raw & 0xFFFF
    elif how == "s16":
        val = raw & 0xFFFF
        val = val - 0x10000 if val >= 0x8000 else val
    elif how == "d10":
        val, scale = raw, 10
    elif how == "d100":
        val, scale = raw, 100
    elif how == "hex":
        return f"0x{raw & 0xFFFFFFFF:X}"
    elif how == "alarm":
        return f"code {raw & 0xFF}, state {(raw >> 16) & 0xFFFF}"
    elif how == "time":
        return raw  # the page formats it (controller clock = local time as UTC)
    else:
        val = raw
    return val / scale if scale != 1 else val


# --------------------------------------------------------------------------
# Table
# --------------------------------------------------------------------------

SAVE_INTERVAL_S = 60


class RegisterTable:
    def __init__(self, log_dir: str | None = None, filename: str = "registers.json") -> None:
        self.path = os.path.join(log_dir, filename) if log_dir else None
        self._lock = threading.Lock()
        # (reg, idx) -> {first, last, raw, stable, stable_t, count, changes, requested}
        self.entries: dict = {}
        self._dirty = False
        self._new = False   # a register was added since the last save
        self._saved = 0.0
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            for e in data.get("registers", []):
                key = (int(e.pop("reg")), int(e.pop("idx")))
                self.entries[key] = e
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def save(self, force: bool = False) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty or (not force and time.time() - self._saved < SAVE_INTERVAL_S
                                   and not self._new):
                return
            rows = [dict(e, reg=r, idx=i) for (r, i), e in sorted(self.entries.items())]
            self._dirty = self._new = False
            self._saved = time.time()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "registers": rows}, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- feeding ------------------------------------------------------------

    def observe(self, ts: float, regs: dict, requested=None) -> None:
        """One report {(reg, idx): raw} at ``ts``; ``requested``: the read list."""
        with self._lock:
            for key, raw in regs.items():
                e = self.entries.get(key)
                if e is None:
                    e = self.entries[key] = {"first": ts, "last": None, "raw": None, "stable": None,
                                             "stable_t": None, "count": 0, "changes": 0}
                    self._new = True
                if e["first"] is None or ts < e["first"]:
                    e["first"] = ts
                if e["last"] is not None and ts <= e["last"]:
                    continue  # older record (log replay after a restart)
                e["count"] += 1
                if raw == e["raw"] and e["stable"] != raw:
                    if e["stable"] is not None:
                        e["changes"] += 1
                    e["stable"] = raw
                if raw == e["stable"]:
                    e["stable_t"] = ts
                e["raw"], e["last"] = raw, ts
                self._dirty = True
            for key in requested or ():
                e = self.entries.get(key)
                if e is None:
                    e = self.entries[key] = {"first": None, "last": None, "raw": None, "stable": None,
                                             "stable_t": None, "count": 0, "changes": 0}
                    self._new = True
                if (e.get("requested") or 0) < ts:
                    e["requested"] = ts
                    self._dirty = True
        self.save()

    # -- reading ------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        with self._lock:
            items = sorted(self.entries.items())
            items = [(k, dict(e)) for k, e in items]
        out = []
        for (reg, idx), e in items:
            label, label_de, how, unit, conf = CATALOG.get(reg, UNKNOWN)
            if idx not in (1, 3, 4) and conf != "unknown":
                conf = "guess"  # only index 1/3/4 (value/min/max) were ever seen
            shown = e["stable"] if e["stable"] is not None else e["raw"]
            value = decode(shown, how)
            name = VALUES.get(reg, {}).get(value) if idx == 1 else None
            out.append({
                "reg": reg, "idx": idx, "label": label, "label_de": label_de, "unit": unit,
                "conf": conf, "decode": how, "range": RANGES.get(reg) if idx == 1 else None,
                "value_name": list(name) if name else None,
                "raw": e["raw"], "stable": e["stable"], "value": value,
                "first": e["first"], "last": e["last"], "stable_t": e.get("stable_t"),
                "count": e["count"], "changes": e["changes"], "requested": e.get("requested"),
            })
        return out

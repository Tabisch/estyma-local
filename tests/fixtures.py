"""Synthetic controller/cloud messages in the recorded layout (fake IDs)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))

import protocol as P  # noqa: E402

BASE = {
    (8548, 1): 256, (8485, 1): 1000040002, (16460, 1): 268, (8484, 1): 541570,
    (16385, 1): 511, (8193, 1): 705, (8195, 1): 60, (8195, 3): 60, (8195, 4): 80,
    (8196, 1): 80, (8197, 1): 257, (8199, 1): 261, (8466, 1): 720, (8467, 1): 540,
    (16451, 1): 1793907412, (16431, 1): 1793904750, (16411, 1): 140,
}


def report(**changes) -> dict:
    regs = dict(BASE)
    for k, v in changes.items():
        reg, idx = k[1:].split("_")
        regs[(int(reg), int(idx))] = v
    return regs


def request_body(regs: dict, frame: int = 33) -> bytes:
    rep = "".join(f"{r},{i},{v};" for (r, i), v in regs.items())
    text = (f"SOFT_VER= 1.12&coreID=11111111,22222222,33333333&PIN=000000&HCM= 1"
            f"&CAN STATUS= 2&NUMER RAMKI={frame}&POBIERZ_ODCZYT= &ZAPISZ_ODCZYT={rep}"
            f"&WIZNET_DEBUG=test;&")
    raw = text.encode()
    return raw + b"\0" * max(0, 3910 - len(raw))


def cloud_reply(writes=()) -> str:
    rl = P.DEFAULT_BASE_READ_LIST[:-7] + P.alarm_slot_regs(11) + P.DEFAULT_BASE_READ_LIST[-7:]
    zap = P.format_writes(writes) if writes else ""
    regs = "".join(f"{r},{i};" for r, i in rl)
    return ("Next Daystat Update: 2026-11-09 00:00:00 (GMT 1) -- " + zap
            + f"ODCZYT&63;{regs}KONIEC&<br>SERVER_INFO=REGISTER,1;DELAY,10;<br><br>"
            "wygenerowano w: 0.0468 sec.")

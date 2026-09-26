"""
Tipo de día según el calendario de circulación de Metrovalencia
(data/calendario_AÑO.json, generado por scripts/extract_calendario.py).

Cada día tiene un COLOR por red (metro / tranvia / l10) y puede tener servicio
NOCTURNO; mismo color = mismo horario. tipo("2026-09-26", 5) -> "verd+nocturn".
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
RED_DE_LINEA = {1: "metro", 2: "metro", 3: "metro", 5: "metro", 7: "metro",
                9: "metro", 4: "tranvia", 6: "tranvia", 8: "tranvia", 10: "l10"}


@lru_cache(maxsize=4)
def _cal(anyo: str) -> dict:
    p = DATA / f"calendario_{anyo}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def tipo(dia: str, linea: int | None) -> str | None:
    red = RED_DE_LINEA.get(linea or 0)
    d = _cal(dia[:4]).get(red or "", {}).get(dia)
    if not d or not d.get("color"):
        return None
    return d["color"] + ("+nocturn" if d.get("nocturn") else "")

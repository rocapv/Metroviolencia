"""
Parser del fragmento HTML devuelto por el endpoint info-estacion de
Metrovalencia (action=formularios_ajax -> info-estacion&id=N).

Extrae por estación la lista de próximas llegadas:
  [{"linea": int, "destino": str, "eta_seg": int, "eta_txt": str, "ocupacion": int|None}]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from html import unescape
from typing import Optional

# Cada bloque de llegada
_ITEM_RE = re.compile(r'item--proximos.*?(?=item--proximos|$)', re.S)
_LINEA_RE = re.compile(r'linea\s+linea-(\d+)')
_DESTINO_RE = re.compile(r'<div class="nombre-estacion">(.*?)</div>', re.S)
_MIN_RE = re.compile(r'<span class="minutos[^"]*">(.*?)</span>', re.S)
_TITULAR_RE = re.compile(
    r'detalle-estacion--titular[^>]*>(.*?)</h3>', re.S
)


@dataclass
class Arrival:
    linea: int
    destino: str
    eta_seg: int
    eta_txt: str
    ocupacion: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


# Destinos mal rotulados por la fuente, por (línea, texto que da la web).
# L1 "Benimaclet": Benimaclet no está en L1 (es de L3/L4/L6/L9); FGV rotula así
# los trenes Castelló -> Bétera (se ha comprobado con los datos: esos trenes
# llegan a Horta Vella y Masies, las paradas anteriores a Bétera). Se corrige al
# parsear para que posiciones, registro de salidas, horarios y mapa vean el
# destino real; sin esto la salida de Castelló se casaba con "Seminari - CEU".
DESTINO_ALIAS: dict[tuple[int, str], str] = {
    (1, "Benimaclet"): "Bétera",
}


def destino_real(linea: int, destino: str) -> str:
    return DESTINO_ALIAS.get((linea, destino), destino)


def parse_eta(txt: str) -> int:
    """'1 min' -> 60 ; '1 h 29 min' -> 5340 ; '2 h' -> 7200. Devuelve segundos."""
    txt = txt.strip().lower()
    horas = 0
    mins = 0
    mh = re.search(r"(\d+)\s*h", txt)
    if mh:
        horas = int(mh.group(1))
    mm = re.search(r"(\d+)\s*min", txt)
    if mm:
        mins = int(mm.group(1))
    if not mh and not mm:
        # a veces sólo un número o "En andana" / "Ara"
        mnum = re.search(r"(\d+)", txt)
        mins = int(mnum.group(1)) if mnum else 0
    return horas * 3600 + mins * 60


def parse_station_name(html: str) -> Optional[str]:
    m = _TITULAR_RE.search(html)
    return _clean(m.group(1)) if m else None


def parse_arrivals(html: str) -> list[Arrival]:
    arrivals: list[Arrival] = []
    for block in _ITEM_RE.findall(html):
        ml = _LINEA_RE.search(block)
        md = _DESTINO_RE.search(block)
        mm = _MIN_RE.search(block)
        if not (ml and md and mm):
            continue
        eta_txt = _clean(mm.group(1))
        linea = int(ml.group(1))
        arrivals.append(
            Arrival(
                linea=linea,
                destino=destino_real(linea, _clean(md.group(1))),
                eta_seg=parse_eta(eta_txt),
                eta_txt=eta_txt,
                ocupacion=None,  # el servidor aún no la expone (campo comentado)
            )
        )
    return arrivals


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "data" / "sample-est25.json"
    )
    html = json.load(open(path, encoding="utf-8"))["html"]
    print("Estación:", parse_station_name(html))
    for a in parse_arrivals(html):
        print(f"  L{a.linea:<2} {a.eta_txt:>10} ({a.eta_seg:>5}s)  -> {a.destino}")

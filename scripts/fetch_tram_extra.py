"""
Tramos de tranvía que solo recorre el SENTIDO CONTRARIO (solo para dibujar).

fetch_tram_tracks.py baja un único sentido por línea (si no, OSM traza los dos
raíles paralelos). Pero en algunos sitios cada sentido va por una CALLE
distinta: en la playa, hacia Dr. Lluch el tranvía va por Eugènia Viñes
(Platja Malva-rosa, Platja les Arenes) y de vuelta por la calle Doctor Lluch
(Cabanyal, Dr. Lluch), que quedaba sin dibujar.

Este script baja las relaciones del sentido contrario y se queda solo con los
trozos que están a más de LEJOS m de cualquier vía ya dibujada (= otra calle,
no el raíl gemelo). Salida: data/tram_tracks_extra.json con el mismo formato
que tram_tracks.json. NO se usa para posicionar trenes (eso sigue en
tram_tracks.json, un trazado por línea): solo lo pinta el frontend.

Uso: python scripts/fetch_tram_extra.py
"""
import json
import math
from pathlib import Path

from fetch_tram_tracks import query

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "tram_tracks.json"
OUT = ROOT / "data" / "tram_tracks_extra.json"

# sentido contrario al de fetch_tram_tracks.RELACIONES
CONTRARIAS = [
    5620871, 5621789, 5621890,   # L4 Dr. Lluch -> (Mas del Rosari / Fira / Ll. Llarga)
    5622028,                     # L6 Marítim -> Tossal del Rei
    5622078,                     # L8 Marítim -> Neptú
]
# L10 no: su trazado ya estaba bien (lo confirmó el usuario el 30/09); el otro
# sentido solo añadía un retal de 170 m junto a Alacant.
LEJOS = 50.0      # m: más lejos que esto de toda vía dibujada = calle distinta
MIN_TRAMO = 80.0  # m: descarta retales
EMPALME = 100.0   # m: distancia máxima para unir un extremo a la vía existente


def _m(a, b):
    return math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 85000)


def _densifica(way, paso=10.0):
    """Puntos cada ~10 m, para medir distancias a la vía sin huecos."""
    out = []
    for a, b in zip(way, way[1:]):
        n = max(1, int(_m(a, b) // paso))
        out += [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n)]
    out.append(way[-1])
    return out


def main() -> int:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    ya = [p for ways in base.values() for w in ways for p in _densifica(w)]
    data = query(CONTRARIAS)
    extra: dict[str, list] = {}
    vistos: set[tuple] = set()          # (línea, way): L4 y L6 comparten tramos
    for rel in data["elements"]:
        if rel["type"] != "relation":
            continue
        ref = rel["tags"].get("ref")
        for m in rel.get("members", []):
            if m["type"] != "way" or "geometry" not in m or (ref, m["ref"]) in vistos:
                continue
            vistos.add((ref, m["ref"]))
            pts = [[round(p["lat"], 6), round(p["lon"], 6)] for p in m["geometry"]]
            # trozos consecutivos de puntos LEJOS de la vía existente, con un
            # punto de más a cada lado para que empalmen con ella
            lejos = [min(_m(p, q) for q in ya) > LEJOS for p in pts]
            i = 0
            while i < len(pts):
                if not lejos[i]:
                    i += 1
                    continue
                j = i
                while j < len(pts) and lejos[j]:
                    j += 1
                tramo = pts[max(0, i - 1):min(len(pts), j + 1)]
                largo = sum(_m(a, b) for a, b in zip(tramo, tramo[1:]))
                if len(tramo) >= 2 and largo >= MIN_TRAMO:
                    # empalma cada extremo con la vía existente más cercana
                    # (si no, queda un hueco donde la vía gemela se separa)
                    for extremo, pos in ((tramo[0], 0), (tramo[-1], len(tramo))):
                        q = min(ya, key=lambda x: _m(extremo, x))
                        if _m(extremo, q) < EMPALME:
                            tramo.insert(pos, [round(q[0], 6), round(q[1], 6)])
                    extra.setdefault(ref, []).append(tramo)
                i = j
    OUT.write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
    print({k: f"{len(v)} tramos, {sum(sum(_m(a, b) for a, b in zip(w, w[1:])) for w in v):.0f} m"
           for k, v in sorted(extra.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

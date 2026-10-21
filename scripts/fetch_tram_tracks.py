"""
Descarga el trazado real de las líneas de TRANVÍA de Metrovalencia (L4, L6, L8,
L10) desde OpenStreetMap (Overpass API) y lo guarda en data/tram_tracks.json.

Metrovalencia no publica polylines limpias de sus tranvías; OSM sí las tiene
(railway=tram, trazadas por la comunidad desde satélite). Formato de salida:
    { "4": [ [[lat,lng],...], ... ], "6": [...], "8": [...], "10": [...] }
(cada línea = lista de tramos de vía).

Uso: python scripts/fetch_tram_tracks.py
"""
import collections
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "tram_tracks.json"
# Variantes por relación OSM (ramales): { "4": { "<rel_id>": [tramos...] } }. Cada
# relación es una ruta completa (p. ej. Ll. Llarga -> Dr. Lluch), sin deduplicar
# entre relaciones: network.py construye con ellas una línea comercial por ramal.
OUT_VARIANTES = ROOT / "data" / "tram_tracks_variants.json"

# Relaciones de ruta OSM por línea. IMPORTANTE: una sola dirección por línea
# (si se mezclan sentidos, OSM traza los dos raíles paralelos = "varios carriles").
# Para L4 (con ramales) se cogen las 3 variantes que van TODAS hacia Dr. Lluch,
# así comparten el tronco (misma vía, se deduplica por id) y aportan cada ramal.
RELACIONES = [
    5620862, 5621891, 5621892,   # L4 -> Dr. Lluch (Mas del Rosari / Fira / Ll. Llarga)
    5621993,                     # L6  Tossal del Rei -> Marítim
    5622077,                     # L8  Neptú -> Marítim
    14166106,                    # L10 Alacant -> Natzaret
]
ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def query(rel_ids: list[int]) -> dict:
    q = (
        "[out:json][timeout:90];\n"
        f"relation(id:{','.join(map(str, rel_ids))});\n"
        "out geom;\n"
    )
    last = None
    for ep in ENDPOINTS:
        try:
            req = urllib.request.Request(
                ep, data=q.encode("utf-8"),
                headers={"User-Agent": "Metroviolencia/1.0"},
            )
            with urllib.request.urlopen(req, timeout=95) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Overpass no respondió: {last}")


def main() -> int:
    data = query(RELACIONES)
    tracks: dict[str, list] = collections.defaultdict(list)
    variantes: dict[str, dict[str, list]] = collections.defaultdict(dict)
    vistos: dict[str, set] = collections.defaultdict(set)  # ref -> way ids
    for rel in data["elements"]:
        if rel["type"] != "relation":
            continue
        ref = rel["tags"].get("ref")
        propias: list = []
        for m in rel.get("members", []):
            if m["type"] != "way" or "geometry" not in m:
                continue
            poly = [[round(p["lat"], 6), round(p["lon"], 6)] for p in m["geometry"]]
            if len(poly) >= 2:
                propias.append(poly)
            wid = m.get("ref")
            if wid in vistos[ref]:      # dedup por ID de vía (tronco compartido)
                continue
            vistos[ref].add(wid)
            if len(poly) >= 2:
                tracks[ref].append(poly)
        variantes[ref][str(rel["id"])] = propias
    OUT.write_text(json.dumps(tracks, ensure_ascii=False), encoding="utf-8")
    # solo las líneas con más de una relación tienen ramales
    OUT_VARIANTES.write_text(json.dumps(
        {ref: v for ref, v in variantes.items() if len(v) > 1}, ensure_ascii=False), encoding="utf-8")
    print({k: f"{len(v)} tramos" for k, v in sorted(tracks.items())})
    print("variantes:", {k: {r: len(w) for r, w in v.items()} for k, v in variantes.items() if len(v) > 1})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# Relaciones de ruta OSM (variantes que cubren ramales) por línea.
RELACIONES = [
    5620862, 5621789, 5621890,   # L4 (Dr. Lluch / Mas del Rosari / Fira / Ll. Llarga)
    5621993,                     # L6 (Tossal del Rei – Marítim)
    5622077,                     # L8 (Neptú – Marítim)
    14166106,                    # L10 (Alacant – Natzaret)
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
    for rel in data["elements"]:
        if rel["type"] != "relation":
            continue
        ref = rel["tags"].get("ref")
        seen = set()
        for m in rel.get("members", []):
            if m["type"] == "way" and "geometry" in m:
                poly = [[round(p["lat"], 6), round(p["lon"], 6)] for p in m["geometry"]]
                if len(poly) < 2:
                    continue
                key = (poly[0][0], poly[0][1], poly[-1][0], poly[-1][1], len(poly))
                if key in seen:
                    continue
                seen.add(key)
                tracks[ref].append(poly)
    OUT.write_text(json.dumps(tracks, ensure_ascii=False), encoding="utf-8")
    print({k: f"{len(v)} tramos" for k, v in sorted(tracks.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

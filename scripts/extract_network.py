"""
Extrae la geometría de la red de Metrovalencia desde el HTML de
https://www.metrovalencia.es/ca/consulta-mapes/ (variable JS `datosMapaGeneral`)
y la vuelca a data/network.json.

Salida:
{
  "colores": { "1": "FEC601", ... },        # color hex por línea
  "lineas":  { "1": [[lat,lng], ...], ... }, # polyline (trazado) por línea
  "estaciones": [
     {"id": 1, "nombre": "...", "lat": .., "lng": .., "lineas": [1,...], "zona": "..."}
  ]
}
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "consulta-mapes.raw.html"
OUT = ROOT / "data" / "network.json"


def extract_object_literal(text: str, start_idx: int) -> str:
    """Devuelve el literal { ... } balanceado que empieza en start_idx."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    raise ValueError("literal no balanceado")


def jsobj_to_json(s: str) -> str:
    """Convierte claves de objeto JS sin comillas (lineas:, estaciones:) a JSON."""
    # Solo afecta a las claves top-level del objeto datosMapaGeneral
    return re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)


def main() -> int:
    html = RAW.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"var\s+datosMapaGeneral\s*=\s*", html)
    if not m:
        print("No se encontró datosMapaGeneral", file=sys.stderr)
        return 1
    brace = html.index("{", m.end())
    literal = extract_object_literal(html, brace)
    data = json.loads(jsobj_to_json(literal))

    colores = data.get("coloresLineas", {})
    lineas_raw = data.get("lineas", {})

    # colores por LÍNEA COMERCIAL (1..10) — vienen en infoEstaciones[].lineas[].color,
    # NO en coloresLineas (que está indexado por segmento de vía interno).
    colores_comercial: dict[str, str] = {}
    lineas = {
        lid: [[p["lat"], p["lng"]] for p in pts]
        for lid, pts in lineas_raw.items()
    }

    # infoEstaciones: dict id -> info (nombre, lineas, zona...)
    info = data.get("infoEstaciones", {})
    est_coords = data.get("estaciones", [])

    # infoEstaciones es una lista paralela a `estaciones` (coords), mismo orden/índice.
    info_list = list(info.values()) if isinstance(info, dict) else list(info)
    estaciones = []
    for i, val in enumerate(info_list):
        if not isinstance(val, dict):
            continue
        sid = val.get("id", i + 1)
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            pass
        coord = est_coords[i] if i < len(est_coords) else {}
        lineas_ids = sorted(
            {int(l["id"]) for l in (val.get("lineas") or []) if str(l.get("id", "")).isdigit()}
        )
        for l in val.get("lineas") or []:
            lid = str(l.get("id", ""))
            if lid.isdigit() and l.get("color"):
                colores_comercial.setdefault(lid, l["color"])
        estaciones.append(
            {
                "id": sid,
                "nombre": val.get("nombre") or val.get("nom") or val.get("denominacion"),
                "lat": coord.get("lat"),
                "lng": coord.get("lng"),
                "lineas": lineas_ids,
                "zona": val.get("zona"),
            }
        )

    out = {
        "colores": colores,
        "colores_comercial": colores_comercial,
        "lineas": lineas,
        "estaciones": estaciones,
        "_meta": {
            "n_lineas": len(lineas),
            "n_estaciones": len(estaciones),
            "n_coords_estaciones": len(est_coords),
        },
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["_meta"], indent=2))
    if estaciones:
        print("ejemplo estación:", json.dumps(estaciones[0], ensure_ascii=False)[:300])
    else:
        print("infoEstaciones keys:", list(info.keys())[:20] if isinstance(info, dict) else type(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

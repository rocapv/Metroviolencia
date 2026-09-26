"""
Extrae el TIPO DE DÍA (color) del calendario de circulación de Metrovalencia.

Metrovalencia publica un PDF por red con los 365 días pintados: el color del
número dice qué horario rige ese día (BLAU / VERD / ROIG, y ANYIL en tranvía) y un recuadro gris
marca servicio nocturno. Mismo color = mismo horario.

El PDF no lleva el color como texto: se sacan las coordenadas de cada número
con `pdftotext -bbox`, se renderiza la página con `pdftoppm` y se mira el color
de los píxeles del propio número.

    python3 scripts/extract_calendario.py [AÑO]   -> data/calendario_AÑO.json

Requiere poppler-utils y Pillow (el python3 del sistema en Raspa lo tiene).
Formato de salida:
    {"metro": {"2026-09-26": {"color": "verd", "nocturn": true}, ...},
     "tranvia": {...}, "l10": {...}, "_fuentes": {...}}
"""
from __future__ import annotations

import calendar
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CA = ROOT / "data" / "ca-bundle.pem"
BASE = "https://www.metrovalencia.es/wp-content/uploads"
# el mes de subida del PDF cambia cada año: se busca en la web (ver _urls)
PAGINA = "https://www.metrovalencia.es/es/consulta-de-horarios-y-planificador/"
REDES = {  # clave -> fragmento del nombre del PDF
    "metro": "Calendario-circulacion-lineas-metro",
    "tranvia": "Calendario-circulacion-lineas-tranvia",
    "l10": "Calendario-circulacion-L10",
}
# qué red rige cada línea comercial
RED_DE_LINEA = {1: "metro", 2: "metro", 3: "metro", 5: "metro", 7: "metro",
                9: "metro", 4: "tranvia", 6: "tranvia", 8: "tranvia", 10: "l10"}
MESES = ["GENER", "FEBRER", "MARÇ", "ABRIL", "MAIG", "JUNY", "JULIOL", "AGOST",
         "SETEMBRE", "OCTUBRE", "NOVEMBRE", "DESEMBRE"]
DPI = 150
_WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                   r'yMax="([\d.]+)">([^<]*)</word>')


def _urls(anyo: int) -> dict[str, str]:
    html = requests.get(PAGINA, headers={"User-Agent": "Mozilla/5.0"}, timeout=30,
                        verify=str(CA) if CA.exists() else True).text
    out = {}
    for red, frag in REDES.items():
        m = re.search(rf'{BASE}/\d{{4}}/\d{{2}}/{frag}-{anyo}\.pdf', html)
        if m:
            out[red] = m.group(0)
    return out


def _clasifica(px: list[tuple[int, int, int]]) -> tuple[str | None, bool]:
    """Píxeles del recuadro de un número -> (color, nocturno)."""
    col = {"blau": 0, "verd": 0, "roig": 0, "anyil": 0}
    gris = 0
    for r, g, b in px:
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn < 18 and 180 < mx < 235:
            gris += 1                       # fondo gris del recuadro nocturno
        if mx < 150 and b - max(r, g) >= 25:
            col["anyil"] += 1               # añil (solo tranvía): azul-gris oscuro
            continue
        if mx - mn < 60:
            continue                        # no es tinta de color
        if r == mx:
            col["roig"] += 1
        elif b == mx:
            col["blau"] += 1
        else:
            col["verd"] += 1
    color = max(col, key=col.get) if max(col.values()) > 5 else None
    return color, gris > 0.25 * len(px)


def extraer(pdf: Path, anyo: int) -> dict[str, dict]:
    xml = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"], check=True,
                         capture_output=True, text=True).stdout
    words = [(float(a), float(b), float(c), float(d), w)
             for a, b, c, d, w in _WORD.findall(xml)]
    ancho = max(w[2] for w in words)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-singlefile",
                        str(pdf), f"{tmp}/p"], check=True)
        img = Image.open(f"{tmp}/p.png").convert("RGB")
    k = DPI / 72.0

    # cabeceras de mes: (mes, columna, y)
    cab = [(MESES.index(w[4]) + 1, 0 if w[0] < ancho / 2 else 1, w[3])
           for w in words if w[4] in MESES]
    if len(cab) != 12:
        raise RuntimeError(f"{pdf.name}: encuentro {len(cab)} meses, no 12")
    out = {}
    for mes, colm, y0 in cab:
        sig = [y for m, c, y in cab if c == colm and y > y0]
        y1 = min(sig) if sig else 1e9
        nums = [w for w in words
                if w[4].isdigit() and (0 if w[0] < ancho / 2 else 1) == colm
                and y0 < w[1] < y1 - 20
                and w[3] - w[1] > 25]   # tamaño de número de día (no la leyenda)
        dias = calendar.monthrange(anyo, mes)[1]
        nums = [w for w in nums if 1 <= int(w[4]) <= dias]
        if len({int(w[4]) for w in nums}) != dias:
            raise RuntimeError(f"{pdf.name} mes {mes}: {len(nums)} números, "
                               f"esperaba {dias}")
        for x0, ya, x1, yb, txt in nums:
            box = img.crop((int(x0 * k), int(ya * k), int(x1 * k), int(yb * k)))
            color, noct = _clasifica(list(box.get_flattened_data()))
            out[f"{anyo}-{mes:02d}-{int(txt):02d}"] = {"color": color, "nocturn": noct}
    return dict(sorted(out.items()))


def main():
    anyo = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    urls = _urls(anyo)
    res: dict = {"_fuentes": urls, "_red_de_linea": RED_DE_LINEA}
    with tempfile.TemporaryDirectory() as tmp:
        for red, url in urls.items():
            pdf = Path(tmp) / f"{red}.pdf"
            pdf.write_bytes(requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                         timeout=60,
                                         verify=str(CA) if CA.exists() else True).content)
            res[red] = extraer(pdf, anyo)
            cuenta: dict = {}
            for v in res[red].values():
                cuenta[v["color"]] = cuenta.get(v["color"], 0) + 1
            print(f"{red}: {len(res[red])} días {cuenta} "
                  f"nocturnos={sum(v['nocturn'] for v in res[red].values())}")
    dst = ROOT / "data" / f"calendario_{anyo}.json"
    dst.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("->", dst)


if __name__ == "__main__":
    main()

"""
Recorregut passat d'un tren (track_id), per a veure'l encara que ja no circule.

Ix de la taula `trenes` (una fila per pasada del poller amb la posició d sobre
la via) i del registre de pas per estació (pasos.registro_tren). Diu com va
acabar:

    viu      -> encara apareix a l'última pasada
    arribat  -> va deixar de veure's a menys de ACABA_M del seu destí (una
                capçalera només anuncia eixides: el tren que arriba desapareix
                a la penúltima estació). S'hi afegeix la cua prevista de
                l'última pasada perquè l'animació arribe fins al final.
    perdut   -> va deixar de veure's a mitjan trajecte: la web hi posa una
                marca roja i hi para l'animació.

També els noms d'URL de les estacions (metro.pr1.es/burjassot).
"""
from __future__ import annotations

import json
import re
import unicodedata

from .pasos import HORIZONTE, registro_tren
from .tracking import llega_a_destino


def slug(nom: str) -> str:
    """Nom d'estació -> tros d'URL: minúscules, sense accents ni símbols
    ('Àngel Guimerà' -> 'angelguimera'). La web fa el mateix (app.js)."""
    s = unicodedata.normalize("NFD", nom)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def slugs(net) -> dict[str, int]:
    """{slug: id d'estació}. Amb la xarxa d'ara no n'hi ha cap de repetit;
    si n'apareguera un, guanya el primer."""
    out: dict[str, int] = {}
    for sid, e in net.estaciones.items():
        out.setdefault(slug(e.nombre if hasattr(e, "nombre") else e["nombre"]), sid)
    return out


def historia(con, net, track_id: int) -> dict | None:
    filas = con.execute(
        "SELECT snapshot_id, ts, linea, destino, color, origen, d, via, waypoints, prox_estacion "
        "FROM trenes WHERE track_id=? ORDER BY ts", (track_id,)).fetchall()
    if not filas:
        return None
    ultima_pasada = con.execute("SELECT MAX(snapshot_id) FROM trenes").fetchone()[0]
    punts = []
    for sid, ts, linea, destino, color, origen, d, via, wps, prox in filas:
        if d is None:
            continue
        punts.append({"ts": ts, "d": round(d, 1), "via": via or linea, "linea": linea,
                      "destino": destino, "origen": bool(origen)})
    if not punts:
        return None
    sid, ts, linea, destino, color, origen, d, via, wps, prox = filas[-1]
    via = via or linea
    wps = json.loads(wps or "[]")
    if sid == ultima_pasada:
        estat = "viu"
    elif llega_a_destino(net, linea, via, destino, wps):
        estat = "arribat"
        # la cua de l'última pasada (on anava i quan), fins al destí
        for w in wps[1:]:
            eta = w.get("eta")
            if eta is None or eta >= HORIZONTE or w.get("d") is None:
                continue
            punts.append({"ts": ts + int(eta), "d": round(w["d"], 1), "via": via, "linea": linea,
                          "destino": destino, "origen": False, "previst": True})
    else:
        estat = "perdut"
    return {
        "id": track_id, "linea": linea, "destino": destino, "color": color, "via": via,
        "estat": estat,
        "fi": {"ts": ts, "d": round(d, 1) if d is not None else None, "via": via,
               "prox": prox, "origen": bool(origen)},
        "punts": punts,
        "estacions": registro_tren(con, net, track_id, ahora=ts),
    }

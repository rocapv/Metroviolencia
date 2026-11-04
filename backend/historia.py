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
                marca i hi para l'animació. `final` diu què li va passar
                (finals.py: continua / cancellat / retirat / sense_dades /
                pendent / indeterminat); fins al 04/11/2026 era sempre una
                marca roja, i quasi sempre el tren havia seguit.

També els noms d'URL de les estacions (metro.pr1.es/burjassot).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

from . import finals
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
    """Recorregut sencer del tren: tots els trossos de la seua cadena
    (finals.cadena), no només l'id demanat. `trossos` = [{id, ini, fi}]."""
    ids = finals.cadena(con, net, track_id)
    filas, trossos = [], []
    for tid in ids:
        f = con.execute(
            "SELECT snapshot_id, ts, linea, destino, color, origen, d, via, waypoints, prox_estacion "
            "FROM trenes WHERE track_id=? ORDER BY ts", (tid,)).fetchall()
        if f:
            filas += f
            trossos.append({"id": tid, "ini": f[0][1], "fi": f[-1][1]})
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
    # Espera en capçalera abans d'eixir: FGV anuncia la pròxima eixida molt
    # abans, i eixe «tren en espera» pot durar hores (03/11: 49 ids més d'1 h,
    # un 16 h a Machado) mentre n'ixen d'altres. No és el tren circulant: el
    # recorregut comença quan es posa en marxa (dos punts seguits fora de capçalera).
    espera = None
    i = next((k for k in range(len(punts)) if not punts[k]["origen"]
              and (k + 1 >= len(punts) or not punts[k + 1]["origen"])), None)
    if i is not None and i > 1:
        espera = {"des_de": punts[0]["ts"], "fins": punts[i - 1]["ts"]}
        punts = punts[i - 1:]
    ult_id = trossos[-1]["id"]
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
    final = finals.veredicte(con, net, ult_id) if estat == "perdut" else None
    # pas per estació de cada tros; les «previstes» d'un tros que no és l'últim
    # les va passar el tros següent: fora
    # registro_tren va per dia natural: un tros que passa de mitjanit es mira
    # dia a dia (si no, es perdia el de abans de les 00:00)
    estacions = []
    for k, t in enumerate(trossos):
        dies = []
        dia = dt.date.fromtimestamp(t["ini"])
        while dia <= dt.date.fromtimestamp(t["fi"]):
            fi_dia = int(dt.datetime.combine(dia + dt.timedelta(days=1), dt.time()).timestamp()) - 1
            dies.append(min(t["fi"], fi_dia))
            dia += dt.timedelta(days=1)
        for j, ahora in enumerate(dies):
            ultim = k == len(trossos) - 1 and j == len(dies) - 1
            for e in registro_tren(con, net, t["id"], ahora=ahora):
                if e.get("previst") and not ultim:
                    continue
                estacions.append(e)
    if espera:
        estacions = [e for e in estacions if e["ts"] >= espera["fins"] - 60]
    estacions.sort(key=lambda e: (e.get("previst", False), e["ts"]))
    return {
        "id": track_id, "id_actual": ult_id, "trossos": trossos,
        "linea": linea, "destino": destino, "color": color, "via": via,
        "estat": estat, "final": final, "espera": espera,
        "fi": {"ts": ts, "d": round(d, 1) if d is not None else None, "via": via,
               "prox": prox, "origen": bool(origen)},
        "punts": punts,
        "estacions": estacions,
    }

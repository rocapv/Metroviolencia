"""
Horario PROGRAMADO de salidas desde cabecera, sacado del planificador público
de Metrovalencia (/es/consulta-de-horarios-y-planificador/, formulario
`horarios-ruta`).

El planificador, dado origen + destino + día, devuelve las horas de paso de los
trenes DIRECTOS origen->destino, pero en una sola lista mezclada ("Trenes con
destino: Aeroport, Torrent Avinguda, ..."), sin decir qué hora es de qué
destino. Para atribuir destino se consulta la cabecera contra CADA destino que
sale de ella y cada hora se asigna al destino más específico que la contiene
(la consulta con menos trenes): un tren a Aeroport sale en Marítim->Alameda y
en Marítim->Aeroport; se queda con Aeroport.

Uso:
    python -m backend.horarios [--dia YYYY-MM-DD]    # descarga y guarda el día
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time

import requests

from .cabeceras import CABECERAS, destinos_de
from .calendario import tipo

PLAN_URL = "https://www.metrovalencia.es/es/consulta-de-horarios-y-planificador/"
NOWP_URL = ("https://www.metrovalencia.es/wp-content/themes/metrovalencia/"
            "functions/ajax-no-wp.php")
PAUSA = 1.0  # s entre consultas: son ~40 al día, sin prisa

SCHEMA = """
-- Salidas PROGRAMADAS desde cabecera (una fila por tren y día).
CREATE TABLE IF NOT EXISTS horario_prog (
    dia        TEXT    NOT NULL,     -- YYYY-MM-DD (día de consulta del planificador)
    origen_id  INTEGER NOT NULL,     -- estación cabecera
    ts_prog    INTEGER NOT NULL,     -- epoch de la hora programada
    hora       TEXT    NOT NULL,     -- HH:MM tal cual la da la web
    destino    TEXT,                 -- destino atribuido (el más específico)
    linea      INTEGER,              -- línea deducida de (cabecera, destino)
    tipo_dia   TEXT,                 -- color del calendario (p.ej. "verd+nocturn")
    PRIMARY KEY (dia, origen_id, ts_prog)
);
-- Qué días/cabeceras se descargaron bien (para no repetir ni dar por vacío).
CREATE TABLE IF NOT EXISTS horario_descargas (
    dia       TEXT    NOT NULL,
    origen_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (dia, origen_id)
);
"""

_TOKEN_RE = re.compile(
    r'name="horarios-ruta".*?name="auth_token" value="([0-9a-f]+)"', re.S)
_HORA_RE = re.compile(r'<p class="hora">(\d{1,2})</p>(.*?)</div>', re.S)
_MIN_RE = re.compile(r'<p class="minuto (?:par|impar)">(\d{1,2})</p>')
_ALT_RE = re.compile(r'js-ruta-opcion')


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def _token(session: requests.Session, headers: dict, verify) -> str:
    r = session.get(PLAN_URL, headers={"User-Agent": headers["User-Agent"]},
                    timeout=20, verify=verify)
    m = _TOKEN_RE.search(r.text)
    if not m:
        raise RuntimeError("no encuentro auth_token de horarios-ruta")
    return m.group(1)


def parse_horas(html: str) -> list[str] | None:
    """HTML de horarios-ruta -> ['HH:MM', ...] de la PRIMERA alternativa, y
    solo si es directa (un único paso, sin transbordo). None si no hay trayecto
    directo o no hay resultados."""
    alts = _ALT_RE.split(html)[1:]
    if not alts:
        return None
    alt = alts[0]
    if alt.count("paso-horario") != 1:   # más de un paso = con transbordo
        return None
    horas = []
    for h, bloque in _HORA_RE.findall(alt):
        for m in _MIN_RE.findall(bloque):
            horas.append(f"{int(h):02d}:{int(m):02d}")
    return horas


def consultar(session, headers, verify, token, origen: int, destino: int,
              dia: str) -> list[str] | None:
    data = (f"auth_token={token}&action=horarios-ruta&origen={origen}"
            f"&destino={destino}&dia={dia}&horaDesde=00:00&horaHasta=23:59")
    r = session.post(NOWP_URL, data={"action": "formularios_ajax", "data": data},
                     headers={**headers, "Referer": PLAN_URL}, timeout=30,
                     verify=verify)
    r.raise_for_status()
    return parse_horas(r.json().get("html") or "")


def _ts(dia: str, hhmm: str) -> int:
    d = dt.datetime.strptime(f"{dia} {hhmm}", "%Y-%m-%d %H:%M")
    return int(time.mktime(d.timetuple()))


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _restar_paso(con, dia: str, cab: dict, por_destino: dict, log) -> bool:
    """Quita de por_destino los trenes que solo PASAN por la cabecera (vienen
    de las cabeceras `paso_de`). Sus horas aquí = salida allí + tiempo de
    trayecto fijo; se busca el desfase (0-90 min) que más horas explica y se
    retiran las que casan a ±1 min. False si aún no hay horario de origen."""
    todas = set().union(*por_destino.values())
    mins = {_min(h): h for h in todas}
    for origen in cab["paso_de"]:
        orig = [_min(r[0]) for r in con.execute(
            "SELECT hora FROM horario_prog WHERE dia=? AND origen_id=?", (dia, origen))]
        if not orig:
            return False

        def casan(k):
            out = set()
            for t in orig:
                for dlt in (0, -1, 1):
                    if t + k + dlt in mins:
                        out.add(mins[t + k + dlt])
                        break
            return out
        k = max(range(0, 91), key=lambda k: len(casan(k)))
        quitar = casan(k)
        for d in por_destino:
            por_destino[d] -= quitar
        log(f"  horario {dia} {cab['nombre']}: -{len(quitar)} de paso "
            f"(desde {origen}, +{k} min)")
    for d in [d for d, v in por_destino.items() if not v]:
        del por_destino[d]
    return bool(por_destino)


def descargar_dia(con: sqlite3.Connection, session, headers, verify,
                  dia: str, net, log=print) -> int:
    """Descarga el horario de todas las cabeceras para `dia`. Idempotente:
    salta las cabeceras ya descargadas. Devuelve nº de salidas nuevas."""
    init_db(con)
    hechas = {r[0] for r in con.execute(
        "SELECT origen_id FROM horario_descargas WHERE dia=?", (dia,))}
    pendientes = [c for c in CABECERAS if c["id"] not in hechas]
    if not pendientes:
        return 0
    token = _token(session, headers, verify)
    total = 0
    for cab in pendientes:
        sid = cab["id"]
        por_destino: dict[str, set[str]] = {}
        lin_de: dict[str, set[int]] = {}
        vistos: dict[str, int] = {}
        for dest_nom, linea, n in destinos_de(con, cab):
            lin_de.setdefault(dest_nom, set()).add(linea)
            vistos[dest_nom] = vistos.get(dest_nom, 0) + n
        for dest_nom in list(lin_de):
            did = net.id_por_nombre(dest_nom)
            if did is None:
                continue
            try:
                horas = consultar(session, headers, verify, token, sid, did, dia)
            except Exception as e:  # noqa: BLE001
                log(f"  horario {cab['nombre']}->{dest_nom}: error {e}")
                horas = None
            time.sleep(PAUSA)
            if horas:
                por_destino[dest_nom] = set(horas)
        if not por_destino:
            log(f"  horario {dia} {cab['nombre']}: sin datos (se reintentará)")
            continue
        if cab.get("paso_de"):
            if not _restar_paso(con, dia, cab, por_destino, log):
                log(f"  horario {dia} {cab['nombre']}: falta el de paso_de (se reintentará)")
                continue
        # destino más específico = el conjunto más pequeño que contiene la hora;
        # a igualdad (la web no distingue, p.ej. Castelló), el más visto en vivo
        orden = sorted(por_destino, key=lambda d: (len(por_destino[d]), -vistos[d]))
        filas = []
        for hhmm in sorted(set().union(*por_destino.values())):
            dest = next(d for d in orden if hhmm in por_destino[d])
            # mismo destino servido por 2 líneas (Aeroport->Machado: L3 y L5):
            # la web no dice cuál es cuál -> línea NULL
            lins = lin_de[dest]
            linea = next(iter(lins)) if len(lins) == 1 else None
            filas.append((dia, sid, _ts(dia, hhmm), hhmm, dest, linea,
                          tipo(dia, linea or min(lins))))
        con.executemany(
            "INSERT OR REPLACE INTO horario_prog(dia,origen_id,ts_prog,hora,destino,"
            "linea,tipo_dia) VALUES (?,?,?,?,?,?,?)", filas)
        con.execute(
            "INSERT OR REPLACE INTO horario_descargas(dia,origen_id,ts,n) VALUES (?,?,?,?)",
            (dia, sid, int(time.time()), len(filas)))
        con.commit()
        total += len(filas)
        log(f"  horario {dia} {cab['nombre']}: {len(filas)} salidas "
            f"({', '.join(f'{d}:{len(v)}' for d, v in por_destino.items())})")
    return total


def main():
    from .network import load_network
    from .poller import DB, HEADERS, VERIFY, make_session

    ap = argparse.ArgumentParser()
    ap.add_argument("--dia", default=dt.date.today().isoformat())
    args = ap.parse_args()
    con = sqlite3.connect(DB, timeout=60)
    n = descargar_dia(con, make_session(), HEADERS, VERIFY, args.dia, load_network())
    print(f"{args.dia}: {n} salidas programadas nuevas")


if __name__ == "__main__":
    main()

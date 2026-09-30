"""
Retrasos EN RUTA: cuándo pasa de verdad cada tren por cada estación frente a su
hora programada en esa estación.

- Horario por estación (`horario_paso`): el planificador oficial consultado de
  cada estación a su VECINA en cada sentido (290 pares): da las horas de paso
  por la estación de todos los trenes que van hacia esa vecina. ~7 min de
  consultas por día, en segundo plano.
- Pasos reales (`pasos_obs`): el mismo seguimiento que las salidas de cabecera
  (salidas.seguir) aplicado a TODAS las estaciones.
- Cruce: cada paso real se casa con la hora programada más cercana de su
  estación y sentido. En los troncos compartidos (varias líneas cada 2-3 min)
  el casamiento es ambiguo; esos pasos se descartan en vez de dar retrasos
  inventados.

    python -m backend.pasos [--dia YYYY-MM-DD]          # resumen por línea
    python -m backend.pasos --horario [--dia ...]       # descarga el horario
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import time

from . import horarios
from .salidas import _rango_dia, seguir

VENTANA = 900          # s: un paso casa con una hora programada a <15 min
MARGEN_AMBIG = 90      # s: si otra hora programada está casi igual de cerca -> ambiguo
TARDE = 180            # s: a partir de aquí el paso "va tarde"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pasos_obs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origen_id   INTEGER NOT NULL,   -- estación observada
    linea       INTEGER NOT NULL,
    destino     TEXT    NOT NULL,
    ts_primera  INTEGER NOT NULL,
    ts_ultima   INTEGER NOT NULL,
    pred_inicial INTEGER NOT NULL,
    ts_pred     INTEGER NOT NULL,
    n_vistas    INTEGER NOT NULL DEFAULT 1,
    estado      TEXT    NOT NULL,   -- en_espera | salida (=pasó) | desaparecida | sin_datos
    ts_salida   INTEGER             -- hora de paso estimada
);
CREATE INDEX IF NOT EXISTS idx_pasos_abiertos ON pasos_obs(estado);
-- seguir(): abiertas = en_espera OR (desaparecida AND ts_ultima reciente)
CREATE INDEX IF NOT EXISTS idx_pasos_obs_estado_ult ON pasos_obs(estado, ts_ultima);
CREATE INDEX IF NOT EXISTS idx_pasos_ts ON pasos_obs(ts_salida);

CREATE TABLE IF NOT EXISTS horario_paso (
    dia        TEXT    NOT NULL,
    estacion_id INTEGER NOT NULL,
    hacia_id   INTEGER NOT NULL,    -- estación vecina (fija el sentido)
    ts_prog    INTEGER NOT NULL,
    hora       TEXT    NOT NULL,
    PRIMARY KEY (dia, estacion_id, hacia_id, ts_prog)
);
CREATE TABLE IF NOT EXISTS horario_paso_desc (
    dia        TEXT    NOT NULL,
    estacion_id INTEGER NOT NULL,
    hacia_id   INTEGER NOT NULL,
    n          INTEGER NOT NULL,
    PRIMARY KEY (dia, estacion_id, hacia_id)
);
"""


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def _todas(net) -> list[dict]:
    return [{"id": e.id, "nombre": e.nombre, "lineas": None} for e in net.estaciones.values()]


def actualizar(con: sqlite3.Connection, ts: int, por_estacion: dict[int, list], net) -> dict:
    return seguir(con, "pasos_obs", ts, _todas(net), por_estacion)


def pares(net) -> list[tuple[int, int]]:
    """(estación, vecina) en ambos sentidos para todas las líneas comerciales."""
    out = set()
    for lc in net.comerciales.values():
        o = [s for _d, s in lc.orden]
        for a, b in zip(o, o[1:]):
            out.add((a, b))
            out.add((b, a))
    return sorted(out)


def descargar_dia(con, session, headers, verify, dia: str, net, log=print) -> int:
    """Horario de paso por estación para `dia` (idempotente, solo lo que falte)."""
    init_db(con)
    hechos = {(r[0], r[1]) for r in con.execute(
        "SELECT estacion_id, hacia_id FROM horario_paso_desc WHERE dia=?", (dia,))}
    faltan = [p for p in pares(net) if p not in hechos]
    if not faltan:
        return 0
    token = horarios._token(session, headers, verify)
    total = 0
    for i, (s, v) in enumerate(faltan):
        try:
            horas = horarios.consultar(session, headers, verify, token, s, v, dia)
        except Exception as e:  # noqa: BLE001
            log(f"  paso {s}->{v}: error {e}")
            token = horarios._token(session, headers, verify)
            continue
        finally:
            time.sleep(horarios.PAUSA)
        horas = horas or []
        con.executemany(
            "INSERT OR IGNORE INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
            " VALUES (?,?,?,?,?)", [(dia, s, v, horarios._ts(dia, h), h) for h in horas])
        con.execute("INSERT OR REPLACE INTO horario_paso_desc(dia,estacion_id,hacia_id,n)"
                    " VALUES (?,?,?,?)", (dia, s, v, len(horas)))
        con.commit()
        total += len(horas)
        if (i + 1) % 50 == 0:
            log(f"  horario de paso {dia}: {i + 1}/{len(faltan)} pares")
    return total


def reprocesar(con: sqlite3.Connection, desde: int, net) -> int:
    """Rehace pasos_obs desde `desde` con las pasadas guardadas (poller PARADO)."""
    init_db(con)
    con.execute("DELETE FROM pasos_obs WHERE ts_primera>=?", (desde,))
    con.execute("UPDATE pasos_obs SET estado='sin_datos' WHERE estado='en_espera'")
    snaps = con.execute("SELECT id, ts FROM snapshots WHERE ts>=? ORDER BY id",
                        (desde,)).fetchall()
    for snap_id, ts in snaps:
        por: dict[int, list] = {}
        for est, lin, dest, eta in con.execute(
                "SELECT estacion_id,linea,destino,eta_seg FROM arrivals WHERE snapshot_id=?",
                (snap_id,)):
            por.setdefault(est, []).append((lin, dest, eta))
        actualizar(con, ts, por, net)
    return len(snaps)


# ------------------------------------------------------------------ cruce

def _vecina(net, linea: int, sid: int, destino: str) -> int | None:
    """Estación siguiente a `sid` en la línea, en el sentido del destino."""
    lc = net.comerciales.get(linea)
    dest = net.id_por_nombre(destino)
    if not lc or dest is None:
        return None
    orden = [s for _d, s in lc.orden]
    if sid not in orden or dest not in orden:
        return None
    i, j = orden.index(sid), orden.index(dest)
    if i == j:
        return None
    return orden[i + 1] if j > i else orden[i - 1]


def retrasos(con, net, dia: str) -> list[dict]:
    """Pasos reales del día casados con su hora programada: una fila por paso
    con retraso_s (+ tarde, − adelantado). Los ambiguos no se devuelven."""
    t0, t1 = _rango_dia(dia)
    prog: dict[tuple[int, int], list[int]] = {}
    for s, v, tp in con.execute(
            "SELECT estacion_id, hacia_id, ts_prog FROM horario_paso WHERE dia=?", (dia,)):
        prog.setdefault((s, v), []).append(tp)
    grupos: dict[tuple[int, int], list[tuple]] = {}
    for sid, linea, destino, tpaso in con.execute(
            "SELECT origen_id, linea, destino, ts_salida FROM pasos_obs "
            "WHERE estado='salida' AND ts_salida>=? AND ts_salida<?", (t0, t1)):
        v = _vecina(net, linea, sid, destino)
        if v is not None and (sid, v) in prog:
            grupos.setdefault((sid, v), []).append((tpaso, linea, destino))
    out = []
    for (sid, v), obs in grupos.items():
        slots = sorted(prog[(sid, v)])
        pares_ = sorted((abs(o[0] - p), i, j) for i, o in enumerate(obs)
                        for j, p in enumerate(slots) if abs(o[0] - p) <= VENTANA)
        usados_o, usados_s = set(), set()
        for _d, i, j in pares_:
            if i in usados_o or j in usados_s:
                continue
            usados_o.add(i); usados_s.add(j)
            tpaso, linea, destino = obs[i]
            d = tpaso - slots[j]
            # ambiguo: otra hora programada casi igual de cerca (troncos compartidos)
            otra = min((abs(tpaso - p) for k, p in enumerate(slots) if k != j), default=1e9)
            if otra < abs(d) + MARGEN_AMBIG:
                continue
            out.append({"estacion_id": sid, "estacion": net.estaciones[sid].nombre,
                        "linea": linea, "destino": destino, "ts_paso": tpaso,
                        "ts_prog": slots[j], "retraso_s": d})
    out.sort(key=lambda f: f["ts_paso"])
    return out


def resumen_lineas(filas: list[dict], desde: int | None = None) -> dict:
    por: dict[int, list[int]] = {}
    for f in filas:
        if desde is None or f["ts_paso"] >= desde:
            por.setdefault(f["linea"], []).append(f["retraso_s"])
    res = {}
    for l, v in sorted(por.items()):
        v.sort()
        res[l] = {"n": len(v), "mediana_s": v[len(v) // 2],
                  "tarde": sum(x >= TARDE for x in v)}
    return res


def main():
    from .network import load_network
    from .poller import DB, HEADERS, VERIFY, make_session

    ap = argparse.ArgumentParser()
    ap.add_argument("--dia", default=dt.date.today().isoformat())
    ap.add_argument("--horario", action="store_true", help="descarga el horario de paso")
    ap.add_argument("--reprocesar", metavar="YYYY-MM-DD",
                    help="rehace los pasos reales desde ese día (poller PARADO)")
    args = ap.parse_args()
    net = load_network()
    if args.reprocesar:
        con = sqlite3.connect(DB, timeout=120)
        n = reprocesar(con, _rango_dia(args.reprocesar)[0] - 3600, net)
        print(f"reprocesadas {n} pasadas")
        return
    if args.horario:
        con = sqlite3.connect(DB, timeout=60)
        n = descargar_dia(con, make_session(), HEADERS, VERIFY, args.dia, net)
        print(f"{args.dia}: {n} horas de paso descargadas")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    filas = retrasos(con, net, args.dia)
    print(f"{args.dia}: {len(filas)} pasos casados con su horario")
    for l, r in resumen_lineas(filas).items():
        print(f"  L{l:<3} {r['n']:>5} pasos  mediana {r['mediana_s']:>5}s  tarde(>=3min) {r['tarde']}")


if __name__ == "__main__":
    main()

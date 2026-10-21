"""
Salidas REALES desde cabecera y registro programado-vs-real.

1) Seguimiento (lo llama el poller en cada pasada, ~1 min):
   en cada cabecera (ver cabeceras.py) todo tren listado es una salida futura.
   Cada tren listado da una salida prevista t = ts + eta. Pasada a pasada se
   casa con la salida abierta más cercana de la misma (cabecera, línea,
   destino). Cuando un tren deja de listarse:
     - si su salida prevista ya había llegado (margen PROX) -> 'salida'
     - si aún faltaba                                       -> 'desaparecida'
       (el tren se cayó del panel antes de salir: señal de cancelación)
   Si reaparece, vuelve a 'en_espera'. Si el hueco sin datos es largo
   (poller caído) -> 'sin_datos': no se sabe qué pasó.

2) Registro (registro(con, dia)): cruza horario_prog con salidas_obs de
   cada cabecera y clasifica cada tren programado:
     realizada | prevista | cancelada | pendiente | sin_datos
   y añade las salidas reales que no estaban en el horario (no_programada).

    python -m backend.salidas [--dia YYYY-MM-DD]   # imprime el resumen
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import time

from .cabeceras import CABECERAS

HORIZONTE = 3600      # s: ETAs a partir de aquí son imprecisas ("1 h") y se ignoran
TOL_CASE = 240        # s: una observación es el mismo tren si su t previsto está a <4 min
PROX = 120            # s: si desaparece con salida prevista a <2 min -> ha salido
HUECO_MAX = 600       # s: más de 10 min sin ver la cabecera -> sin_datos
REAPARECE = 900       # s: una desaparecida puede volver a casarse durante 15 min
# cruce programado<->real
VENTANA_ANTES = -60   # s: un tren no sale adelantado; 60 s por el redondeo al minuto
VENTANA_DESPUES = 1800  # s: ... y hasta 30 min después
PEN_DESTINO = 300     # s de penalización si el destino no coincide
PEN_LINEA = 900       # s de penalización si la línea (conocida) no coincide
GRACIA = 900          # s: pasada la hora programada + esto, sin salida -> cancelada

SCHEMA = """
CREATE TABLE IF NOT EXISTS salidas_obs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origen_id   INTEGER NOT NULL,
    linea       INTEGER NOT NULL,
    destino     TEXT    NOT NULL,
    ts_primera  INTEGER NOT NULL,   -- primera vez listado
    ts_ultima   INTEGER NOT NULL,   -- última vez listado
    pred_inicial INTEGER NOT NULL,  -- salida prevista la primera vez
    ts_pred     INTEGER NOT NULL,   -- salida prevista la última vez
    n_vistas    INTEGER NOT NULL DEFAULT 1,
    estado      TEXT    NOT NULL,   -- en_espera | salida | desaparecida | sin_datos
    ts_salida   INTEGER             -- estimada (solo si estado=salida)
);
CREATE INDEX IF NOT EXISTS idx_sal_abiertas ON salidas_obs(estado, origen_id);
-- seguir(): abiertas = en_espera OR (desaparecida AND ts_ultima reciente)
CREATE INDEX IF NOT EXISTS idx_salidas_obs_estado_ult ON salidas_obs(estado, ts_ultima);
CREATE INDEX IF NOT EXISTS idx_sal_pred ON salidas_obs(origen_id, ts_pred);
"""


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def seguir(con: sqlite3.Connection, tabla: str, ts: int, estaciones: list[dict],
           por_estacion: dict[int, list]) -> dict:
    """Seguimiento genérico tren-a-tren de las llegadas anunciadas en un conjunto
    de estaciones. `estaciones`: [{id, nombre, lineas}] (lineas None = todas).
    En `tabla` (salidas_obs o pasos_obs, mismo esquema; `origen_id` es la
    estación observada) cada tren anunciado queda en_espera hasta que deja de
    anunciarse: 'salida' si ya le tocaba (salió/pasó), 'desaparecida' si no."""
    cont = {"salen": 0, "nuevas": 0, "desaparecen": 0}
    ids = [e["id"] for e in estaciones if e["id"] in por_estacion]
    if not ids:
        return cont
    abiertas_por: dict[int, dict[tuple, list]] = {}
    for r in con.execute(
            f"SELECT id,linea,destino,ts_pred,ts_ultima,estado,origen_id FROM {tabla} "
            f"WHERE estado='en_espera' OR (estado='desaparecida' AND ts_ultima>=?)",
            (ts - REAPARECE,)):
        abiertas_por.setdefault(r[6], {}).setdefault((r[1], r[2]), []).append(list(r[:6]))
    for est in estaciones:
        sid = est["id"]
        if sid not in por_estacion:
            continue          # sin lectura de esta estación: no se envejece nada
        lineas = est.get("lineas")
        obs: dict[tuple, list[int]] = {}
        for linea, destino, eta in por_estacion[sid]:
            # ETA >= 1 h llega como "1 h"/"2 h" sin minutos (error de hasta
            # 59 min): no sirve para casar. Todo tren entra igualmente al
            # bajar de 60 min, con resolución de minuto.
            if eta >= HORIZONTE:
                continue
            if (lineas is None or linea in lineas) and destino and destino != est["nombre"]:
                obs.setdefault((linea, destino), []).append(ts + eta)
        abiertas = abiertas_por.get(sid, {})

        for clave in set(obs) | set(abiertas):
            preds = sorted(obs.get(clave, []))
            ents = abiertas.get(clave, [])
            # emparejamiento voraz por cercanía de la salida prevista
            pares = sorted(
                (abs(p - e[3]), i, j) for i, p in enumerate(preds)
                for j, e in enumerate(ents) if abs(p - e[3]) <= TOL_CASE)
            usadas_p, usadas_e = set(), set()
            for _d, i, j in pares:
                if i in usadas_p or j in usadas_e:
                    continue
                usadas_p.add(i); usadas_e.add(j)
                con.execute(
                    f"UPDATE {tabla} SET ts_ultima=?, ts_pred=?, n_vistas=n_vistas+1,"
                    " estado='en_espera' WHERE id=?", (ts, preds[i], ents[j][0]))
            for i, p in enumerate(preds):
                if i not in usadas_p:
                    con.execute(
                        f"INSERT INTO {tabla}(origen_id,linea,destino,ts_primera,"
                        "ts_ultima,pred_inicial,ts_pred,estado) VALUES (?,?,?,?,?,?,?,"
                        "'en_espera')", (sid, clave[0], clave[1], ts, ts, p, p))
                    cont["nuevas"] += 1
            for j, e in enumerate(ents):
                if j in usadas_e or e[5] != "en_espera":
                    continue
                eid, _l, _d, pred, ultima, _e = e
                if ts - ultima > HUECO_MAX:
                    con.execute(f"UPDATE {tabla} SET estado='sin_datos' WHERE id=?", (eid,))
                elif pred <= ts + PROX:
                    # salió entre la última vez que se vio y ahora
                    salida = max(ultima, min(pred, ts))
                    con.execute(f"UPDATE {tabla} SET estado='salida', ts_salida=?"
                                " WHERE id=?", (salida, eid))
                    cont["salen"] += 1
                else:
                    con.execute(f"UPDATE {tabla} SET estado='desaparecida' WHERE id=?",
                                (eid,))
                    cont["desaparecen"] += 1
    con.commit()
    return cont


def actualizar(con: sqlite3.Connection, ts: int, por_estacion: dict[int, list]) -> dict:
    """Salidas desde cabecera. por_estacion: {estacion_id: [(linea, destino,
    eta_seg), ...]} con SOLO las estaciones leídas bien en esta pasada."""
    return seguir(con, "salidas_obs", ts, CABECERAS, por_estacion)


# ---------------------------------------------------------------- registro

def _rango_dia(dia: str) -> tuple[int, int]:
    d0 = dt.datetime.strptime(dia, "%Y-%m-%d")
    t0 = int(time.mktime(d0.timetuple()))
    t1 = int(time.mktime((d0 + dt.timedelta(days=1)).timetuple()))
    return t0, t1


def _cobertura(con, sid: int, t0: int, t1: int) -> list[int]:
    """Instantes (ordenados) en que la estación se leyó bien: pasadas del
    poller menos las que fallaron en esa estación. No usa `arrivals` (que se
    purga), así un día antiguo sigue distinguiendo cancelado de sin datos."""
    try:
        malas = {r[0] for r in con.execute(
            "SELECT ts FROM lecturas_fallidas WHERE estacion_id=? AND ts>=? AND ts<?",
            (sid, t0, t1))}
    except sqlite3.OperationalError:
        malas = set()
    return [r[0] for r in con.execute(
        "SELECT ts FROM snapshots WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))
        if r[0] not in malas]


def _cubierto(cob: list[int], t: int, margen: int = HUECO_MAX) -> bool:
    import bisect
    i = bisect.bisect_left(cob, t - margen)
    return i < len(cob) and cob[i] <= t + margen


def registro(con: sqlite3.Connection, dia: str, ahora: int | None = None) -> list[dict]:
    """Una fila por salida programada (y por salida real no programada)."""
    ahora = ahora or int(time.time())
    t0, t1 = _rango_dia(dia)
    out: list[dict] = []
    for cab in CABECERAS:
        sid = cab["id"]
        prog = [dict(zip(("ts_prog", "hora", "destino", "linea", "tipo_dia"), r))
                for r in con.execute(
                    "SELECT ts_prog,hora,destino,linea,tipo_dia FROM horario_prog "
                    "WHERE dia=? AND origen_id=? ORDER BY ts_prog", (dia, sid))]
        obs = [dict(zip(("id", "linea", "destino", "estado", "ts_salida", "ts_pred",
                         "pred_inicial", "ts_primera"), r))
               for r in con.execute(
                   "SELECT id,linea,destino,estado,ts_salida,ts_pred,pred_inicial,"
                   "ts_primera FROM salidas_obs WHERE origen_id=? AND ts_pred>=? "
                   "AND ts_pred<?", (sid, t0 + VENTANA_ANTES, t1 + VENTANA_DESPUES))]
        for o in obs:
            o["t"] = o["ts_salida"] if o["estado"] == "salida" else o["ts_pred"]
        pares = []
        for i, p in enumerate(prog):
            for j, o in enumerate(obs):
                d = o["t"] - p["ts_prog"]
                if not (VENTANA_ANTES <= d <= VENTANA_DESPUES):
                    continue
                coste = abs(d)
                if o["destino"] != p["destino"]:
                    coste += PEN_DESTINO
                if p["linea"] and o["linea"] != p["linea"]:
                    coste += PEN_LINEA
                pares.append((coste, i, j))
        pares.sort()
        cas_p: dict[int, int] = {}
        usadas: set[int] = set()
        for _c, i, j in pares:
            if i in cas_p or j in usadas:
                continue
            cas_p[i] = j
            usadas.add(j)
        cob = _cobertura(con, sid, t0 - HUECO_MAX, t1 + VENTANA_DESPUES)
        for i, p in enumerate(prog):
            fila = {"cabecera": cab["nombre"], "origen_id": sid, **p,
                    "real": None, "retraso_s": None, "linea_real": None,
                    "destino_real": None}
            if i in cas_p:
                o = obs[cas_p[i]]
                fila.update(linea_real=o["linea"], destino_real=o["destino"],
                            real=o["t"], retraso_s=o["t"] - p["ts_prog"])
                fila["estado"] = {"salida": "realizada", "en_espera": "prevista",
                                  "desaparecida": "cancelada",
                                  "sin_datos": "sin_datos"}[o["estado"]]
            elif p["ts_prog"] + GRACIA > ahora:
                fila["estado"] = "pendiente"
            elif _cubierto(cob, p["ts_prog"]):
                fila["estado"] = "cancelada"      # había datos y el tren nunca apareció
            else:
                fila["estado"] = "sin_datos"
            out.append(fila)
        for j, o in enumerate(obs):
            if j in usadas or o["estado"] != "salida" or not (t0 <= o["t"] < t1):
                continue
            if not prog:
                continue   # sin horario descargado para esta cabecera: no se juzga
            out.append({"cabecera": cab["nombre"], "origen_id": sid, "ts_prog": None,
                        "hora": None, "destino": None, "linea": None, "tipo_dia": None,
                        "real": o["t"], "retraso_s": None, "linea_real": o["linea"],
                        "destino_real": o["destino"], "estado": "no_programada"})
    out.sort(key=lambda f: (f["ts_prog"] or f["real"]))
    return out


def resumen(filas: list[dict]) -> dict:
    tot: dict[str, int] = {}
    for f in filas:
        tot[f["estado"]] = tot.get(f["estado"], 0) + 1
    ret = [f["retraso_s"] for f in filas if f["estado"] == "realizada"]
    ret.sort()
    return {"por_estado": tot,
            "retraso_mediano_s": ret[len(ret) // 2] if ret else None,
            "retraso_medio_s": round(sum(ret) / len(ret)) if ret else None}


def reprocesar(con: sqlite3.Connection, desde: int) -> int:
    """Rehace salidas_obs desde `desde` a partir de las pasadas guardadas en
    `arrivals` (histórico). Borra lo que hubiera desde ese instante. Parar el
    poller antes: si no, las dos escrituras se pisan."""
    init_db(con)
    con.execute("DELETE FROM salidas_obs WHERE ts_primera>=?", (desde,))
    con.execute("UPDATE salidas_obs SET estado='sin_datos' WHERE estado='en_espera'")
    ids = ",".join(str(c["id"]) for c in CABECERAS)
    snaps = con.execute("SELECT id, ts FROM snapshots WHERE ts>=? ORDER BY id",
                        (desde,)).fetchall()
    for snap_id, ts in snaps:
        por: dict[int, list] = {}
        for est, lin, dest, eta in con.execute(
                f"SELECT estacion_id,linea,destino,eta_seg FROM arrivals "
                f"WHERE snapshot_id=? AND estacion_id IN ({ids})", (snap_id,)):
            por.setdefault(est, []).append((lin, dest, eta))
        actualizar(con, ts, por)
    return len(snaps)


def main():
    from .poller import DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--dia", default=dt.date.today().isoformat())
    ap.add_argument("--reprocesar", metavar="YYYY-MM-DD",
                    help="rehace las salidas reales desde ese día (poller PARADO)")
    args = ap.parse_args()
    if args.reprocesar:
        con = sqlite3.connect(DB, timeout=60)
        n = reprocesar(con, _rango_dia(args.reprocesar)[0] - 3600)
        print(f"reprocesadas {n} pasadas")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    filas = registro(con, args.dia)
    print(args.dia, resumen(filas))
    for cab in CABECERAS:
        fs = [f for f in filas if f["origen_id"] == cab["id"]]
        r = resumen(fs)
        print(f"  {cab['nombre']:<24} {r['por_estado']}  "
              f"retraso mediano {r['retraso_mediano_s']}s")


if __name__ == "__main__":
    main()

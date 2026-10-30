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
import json
import sqlite3
import threading
import time

from . import horarios
from .salidas import HORIZONTE, _rango_dia, seguir

VENTANA = 900          # s: un paso casa con una hora programada a <15 min
MARGEN_AMBIG = 90      # s: si otra hora programada está casi igual de cerca -> ambiguo
INCOHERENTE = 150     # s: desvío máximo del retraso mediano frente a paradas contiguas
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
-- Mismo tipo de día = mismo horario: las horas de paso se guardan UNA vez por
-- tipo (el día "representante", rep=dia) y los demás días solo apuntan a él.
-- `firma` = hash del contenido; el ts de cada hora se recalcula para el día
-- pedido (horas_dia), así el cambio de hora no descuadra nada.
CREATE TABLE IF NOT EXISTS horario_paso_rep (
    dia   TEXT PRIMARY KEY,
    rep   TEXT NOT NULL,
    firma TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paso_rep_firma ON horario_paso_rep(firma);
-- Resumen de retrasos de días cerrados (lo que queda cuando pasos_obs se purga).
CREATE TABLE IF NOT EXISTS retards_dia (
    dia       TEXT    NOT NULL,
    linea     INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    mediana_s INTEGER NOT NULL,
    p90_s     INTEGER NOT NULL,
    tarde     INTEGER NOT NULL,
    PRIMARY KEY (dia, linea)
);
CREATE TABLE IF NOT EXISTS retards_estacio (
    dia         TEXT    NOT NULL,
    linea       INTEGER NOT NULL,
    estacion_id INTEGER NOT NULL,
    hacia_id    INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    mediana_s   INTEGER NOT NULL,
    p90_s       INTEGER NOT NULL,
    tarde       INTEGER NOT NULL,
    PRIMARY KEY (dia, linea, estacion_id, hacia_id)
);
-- Días ya resumidos (aunque no casaran ningún paso): la purga no los repite.
CREATE TABLE IF NOT EXISTS retards_resumits (
    dia TEXT PRIMARY KEY,
    n   INTEGER NOT NULL,
    ts  INTEGER NOT NULL
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
    dedupe_dia(con, dia, net, log)
    return total


# ------------------------------------------------- horario por tipo de día

def rep_de(con, dia: str) -> str:
    """Día representante cuyo horario vale para `dia` (él mismo si no está mapeado)."""
    try:
        r = con.execute("SELECT rep FROM horario_paso_rep WHERE dia=?", (dia,)).fetchone()
    except sqlite3.OperationalError:       # base sin la tabla todavía
        return dia
    return r[0] if r else dia


def horas_dia(con, dia: str) -> list[tuple[int, int, int]]:
    """[(estación, vecina, ts_prog)] del horario de paso de `dia`, con el ts
    recalculado para ESE día a partir de la hora (el representante puede ser
    otro día, incluso con distinto horario de verano)."""
    rep = rep_de(con, dia)
    filas = con.execute("SELECT estacion_id, hacia_id, hora FROM horario_paso WHERE dia=?",
                        (rep,)).fetchall()
    return [(s, v, horarios._ts(dia, h)) for s, v, h in filas]


def _firma_dia(con, dia: str) -> str | None:
    import hashlib
    filas = con.execute("SELECT estacion_id, hacia_id, hora FROM horario_paso WHERE dia=? "
                        "ORDER BY estacion_id, hacia_id, hora", (dia,)).fetchall()
    if not filas:
        return None
    h = hashlib.sha1()
    for s, v, hora in filas:
        h.update(f"{s},{v},{hora};".encode())
    return h.hexdigest()


def dedupe_dia(con, dia: str, net, log=print) -> str | None:
    """Si el horario de `dia` (completo) es idéntico al de un representante ya
    guardado, deja solo el puntero y borra sus filas. Devuelve el representante
    (o None si el día no está completo)."""
    r = con.execute("SELECT rep FROM horario_paso_rep WHERE dia=?", (dia,)).fetchone()
    if r:
        return r[0]
    n_desc = con.execute("SELECT count(*) FROM horario_paso_desc WHERE dia=?", (dia,)).fetchone()[0]
    if n_desc < len(pares(net)):
        return None                               # aún faltan pares: otro día
    firma = _firma_dia(con, dia)
    if firma is None:
        return None
    r = con.execute("SELECT rep FROM horario_paso_rep WHERE firma=? AND rep=dia LIMIT 1",
                    (firma,)).fetchone()
    if r and r[0] != dia:
        con.execute("INSERT INTO horario_paso_rep(dia,rep,firma) VALUES (?,?,?)", (dia, r[0], firma))
        cur = con.execute("DELETE FROM horario_paso WHERE dia=?", (dia,))
        con.commit()
        log(f"  horario de paso {dia}: igual que {r[0]}, {cur.rowcount} filas liberadas")
        return r[0]
    con.execute("INSERT INTO horario_paso_rep(dia,rep,firma) VALUES (?,?,?)", (dia, dia, firma))
    con.commit()
    return dia


def dedupe_todo(con, net, log=print) -> dict:
    """Migración: aplica dedupe_dia a todos los días completos (poller en marcha vale)."""
    init_db(con)
    dias = [r[0] for r in con.execute("SELECT DISTINCT dia FROM horario_paso ORDER BY dia")]
    res = {"dias": len(dias), "representantes": 0, "mapeados": 0, "incompletos": 0}
    for d in dias:
        rep = dedupe_dia(con, d, net, log)
        if rep is None:
            res["incompletos"] += 1
        elif rep == d:
            res["representantes"] += 1
        else:
            res["mapeados"] += 1
        time.sleep(0.05)
    return res


# ------------------------------------------------- resumen de días cerrados

def _stats(v: list[int]) -> tuple[int, int, int]:
    v = sorted(v)
    return v[len(v) // 2], v[min(len(v) - 1, int(len(v) * 0.9))], sum(x >= TARDE for x in v)


def guardar_resumen(con, net, dia: str) -> int:
    """Calcula los retrasos de `dia` y guarda el resumen por línea y por parada.
    Es lo que sobrevive a la purga de pasos_obs. Devuelve el nº de pasos casados."""
    filas = retrasos(con, net, dia)
    por_l: dict[int, list[int]] = {}
    por_e: dict[tuple, list[int]] = {}
    for f in filas:
        por_l.setdefault(f["linea"], []).append(f["retraso_s"])
        por_e.setdefault((f["linea"], f["estacion_id"], f["hacia_id"]), []).append(f["retraso_s"])
    con.execute("DELETE FROM retards_dia WHERE dia=?", (dia,))
    con.execute("DELETE FROM retards_estacio WHERE dia=?", (dia,))
    con.executemany("INSERT INTO retards_dia(dia,linea,n,mediana_s,p90_s,tarde) VALUES (?,?,?,?,?,?)",
                    [(dia, l, len(v), *_stats(v)) for l, v in por_l.items()])
    con.executemany("INSERT INTO retards_estacio(dia,linea,estacion_id,hacia_id,n,mediana_s,p90_s,tarde)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(dia, l, s, h, len(v), *_stats(v)) for (l, s, h), v in por_e.items()])
    con.execute("INSERT OR REPLACE INTO retards_resumits(dia,n,ts) VALUES (?,?,?)",
                (dia, len(filas), int(time.time())))
    con.commit()
    return len(filas)


def resumen_guardado(con, dia: str) -> dict | None:
    """Resumen por línea de un día ya resumido (mismo formato que resumen_lineas),
    o None si el día no se ha resumido (aún tiene sus pasos crudos)."""
    try:
        if not con.execute("SELECT 1 FROM retards_resumits WHERE dia=?", (dia,)).fetchone():
            return None
        return {r[0]: {"n": r[1], "mediana_s": r[2], "p90_s": r[3], "tarde": r[4]}
                for r in con.execute("SELECT linea,n,mediana_s,p90_s,tarde FROM retards_dia "
                                     "WHERE dia=? ORDER BY linea", (dia,))}
    except sqlite3.OperationalError:
        return None


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

def _vecina(net, linea: int, sid: int, destino: str,
            sentido: int | None = None) -> int | None:
    """Estación siguiente a `sid` en la línea, en el sentido del destino. Si el
    destino no está en el trazado de la línea (pasó con la L1 rotulada
    "Benimaclet" = Bétera, hoy corregida en parser.DESTINO_ALIAS; queda como
    red para el histórico y para rótulos nuevos), usa `sentido` (+1 = orden
    creciente)."""
    # la estación puede estar solo en una variante de ramal (L4: Ll. Llarga, Fira)
    lc = next((net.comerciales[v] for v in net.vias_de(linea)
               if v in net.comerciales and sid in net.comerciales[v].estaciones), None)
    if not lc:
        return None
    orden = [s for _d, s in lc.orden]
    i = orden.index(sid)
    dest = net.id_por_nombre(destino)
    if dest in orden:
        j = orden.index(dest)
        if i == j:
            return None
        sentido = 1 if j > i else -1
    if sentido not in (1, -1):
        return None
    k = i + sentido
    return orden[k] if 0 <= k < len(orden) else None


_sent_cache: dict[str, dict] = {}


def sentidos_inferidos(con, net, dia: str) -> dict[tuple[int, str], int]:
    """Sentido de los (línea, destino) cuyo destino no está en el trazado de la
    línea, deducido de los pasos reales: pasos del mismo (línea, destino) por
    estaciones VECINAS con 1-6 min de diferencia votan +1/-1."""
    if dia in _sent_cache:
        return _sent_cache[dia]
    t0, t1 = _rango_dia(dia)
    idx = {l: {s: i for i, (_d, s) in enumerate(lc.orden)} for l, lc in net.comerciales.items()}
    fuera = set()
    por: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for sid, linea, destino, tp in con.execute(
            "SELECT origen_id, linea, destino, ts_salida FROM pasos_obs "
            "WHERE +estado='salida' AND ts_salida>=? AND ts_salida<?", (t0 - 86400, t1)):
        d = net.id_por_nombre(destino)
        if linea in idx and d not in idx[linea] and sid in idx[linea]:
            fuera.add((linea, destino))
            por.setdefault((linea, destino), []).append((tp, idx[linea][sid]))
    res = {}
    for k, lst in por.items():
        lst.sort()
        votos = 0
        for (ta, ia), (tb, ib) in zip(lst, lst[1:]):
            if abs(ib - ia) == 1 and 60 <= tb - ta <= 360:
                votos += 1 if ib > ia else -1
        if abs(votos) >= 5:
            res[k] = 1 if votos > 0 else -1
    _sent_cache.clear()
    _sent_cache[dia] = res
    return res


def pares_incoherentes(filas: list[dict], minimo: int = 8,
                       extremos: set[int] = frozenset()) -> set[tuple[int, int]]:
    """(estación, vecina) cuyo retraso mediano del día se aparta más de
    INCOHERENTE s del de sus estaciones contiguas en el MISMO sentido. Un tren
    no gana ni pierde varios minutos de una parada a la siguiente: si pasa, lo
    que falla es el horario de esa parada (p. ej. Canyamelar en el bucle de L6,
    donde el planificador da a los dos sentidos las horas de uno solo)."""
    import statistics as st
    por: dict[tuple[int, int], list[int]] = {}
    for f in filas:
        por.setdefault((f["estacion_id"], f["hacia_id"]), []).append(f["retraso_s"])
    med = {k: st.median(v) for k, v in por.items() if len(v) >= minimo}
    malos = set()
    for (s, v), m in med.items():
        if s in extremos:
            continue       # donde empiezan/acaban trenes el retraso "salta" de verdad
        vecinos = [mm for (a, b), mm in med.items()
                   if (b == s and a != v) or (a == v and b != s)]
        if vecinos and abs(m - st.median(vecinos)) > INCOHERENTE:
            malos.add((s, v))
    return malos


def _extremos(net, filas: list[dict]) -> set[int]:
    """Estaciones donde empiezan o acaban trenes: destinos vistos y retornos."""
    noms = {f["destino"] for f in filas} | set(net.estaciones_retorno())
    return {i for i in (net.id_por_nombre(n) for n in noms) if i is not None}


_incoh_cache: dict[str, set] = {}
_incoh_pend: set[str] = set()
_incoh_lock = threading.Lock()


def incoherentes(con, net, dia: str) -> set[tuple[int, int]]:
    """Pares incoherentes según el día ANTERIOR (cerrado), para el cálculo en
    vivo. Tarda ~20 s en la Raspa, así que se calcula en un hilo aparte una vez
    al día; mientras tanto devuelve un conjunto vacío (no bloquea /api/trains)."""
    if dia in _incoh_cache:
        return _incoh_cache[dia]
    with _incoh_lock:
        if dia not in _incoh_pend:
            _incoh_pend.add(dia)
            threading.Thread(target=_calcula_incoherentes, args=(net, dia),
                             daemon=True).start()
    return set()


def _calcula_incoherentes(net, dia: str) -> None:
    from .poller import DB
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
        ayer = (dt.date.fromisoformat(dia) - dt.timedelta(days=1)).isoformat()
        filas = _crudos(c, net, ayer)
        res = pares_incoherentes(filas, extremos=_extremos(net, filas))
        c.close()
        _incoh_cache.clear()
        _incoh_cache[dia] = res
    finally:
        with _incoh_lock:
            _incoh_pend.discard(dia)


def retrasos(con, net, dia: str) -> list[dict]:
    """Pasos reales del día casados con su hora programada: una fila por paso
    con retraso_s (+ tarde, − adelantado). Los ambiguos y los de paradas con
    horario incoherente no se devuelven."""
    filas = _crudos(con, net, dia)
    malos = pares_incoherentes(filas, extremos=_extremos(net, filas))
    return [f for f in filas if (f["estacion_id"], f["hacia_id"]) not in malos]


def _crudos(con, net, dia: str) -> list[dict]:
    t0, t1 = _rango_dia(dia)
    prog: dict[tuple[int, int], list[int]] = {}
    for s, v, tp in horas_dia(con, dia):
        prog.setdefault((s, v), []).append(tp)
    sent = sentidos_inferidos(con, net, dia)
    grupos: dict[tuple[int, int], list[tuple]] = {}
    for sid, linea, destino, tpaso in con.execute(
            "SELECT origen_id, linea, destino, ts_salida FROM pasos_obs "
            "WHERE +estado='salida' AND ts_salida>=? AND ts_salida<?", (t0, t1)):
        v = _vecina(net, linea, sid, destino, sent.get((linea, destino)))
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
            out.append({"estacion_id": sid, "hacia_id": v,
                        "estacion": net.estaciones[sid].nombre,
                        "linea": linea, "destino": destino, "ts_paso": tpaso,
                        "ts_prog": slots[j], "retraso_s": d})
    out.sort(key=lambda f: f["ts_paso"])
    return out


_prog_cache: dict[str, dict] = {}


def _prog_dia(con, dia: str) -> dict[tuple[int, int], list[int]]:
    """Horario de paso del día en memoria ({(estación, vecina): [ts...]})."""
    if dia not in _prog_cache:
        prog: dict[tuple[int, int], list[int]] = {}
        for s, v, tp in horas_dia(con, dia):
            prog.setdefault((s, v), []).append(tp)
        for v in prog.values():
            v.sort()
        if prog:                      # no cachear un día aún sin descargar
            _prog_cache.clear()
            _prog_cache[dia] = prog
        return prog
    return _prog_cache[dia]


def _casar_slot(slots: list[int], real: int) -> tuple[int, int] | None:
    """(ts_prog, retraso_s) de la hora programada más cercana a `real` entre
    `slots` (ordenados), o None si está a más de VENTANA o si otra hora está
    casi igual de cerca (MARGEN_AMBIG: tronco compartido, no se sabe cuál es)."""
    import bisect
    if not slots:
        return None
    i = bisect.bisect_left(slots, real)
    cands = sorted(slots[max(0, i - 2):i + 2], key=lambda p: abs(real - p))
    d = real - cands[0]
    if abs(d) > VENTANA:
        return None
    if len(cands) > 1 and abs(real - cands[1]) < abs(d) + MARGEN_AMBIG:
        return None
    return cands[0], d


def _hhmm(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def retraso_vivo(con, net, tren: dict, ts: int, max_paradas: int = 4) -> dict | None:
    """Retraso AHORA de un tren en vivo: su llegada prevista a las próximas
    paradas (ts + eta de los waypoints) frente a la hora programada de paso en
    esa parada y sentido. Usa la primera parada donde el casamiento no es
    ambiguo. None si no hay horario o todo es ambiguo."""
    prog = _prog_dia(con, dt.date.fromtimestamp(ts).isoformat())
    if not prog:
        return None
    dudosos = incoherentes(con, net, dt.date.fromtimestamp(ts).isoformat())
    wps = tren.get("waypoints") or []
    # sentido del tren = su avance sobre la vía (d creciente = orden creciente)
    sentido = None
    if len(wps) >= 2 and abs(wps[-1]["d"] - wps[0]["d"]) > 1:
        sentido = 1 if wps[-1]["d"] > wps[0]["d"] else -1
    probadas = 0
    for wp in wps:
        nom = wp.get("nom")
        if not nom or wp.get("eta") is None or wp["eta"] >= 3600:
            continue
        probadas += 1
        if probadas > max_paradas:
            break
        sid = net.id_por_nombre(nom)
        v = (_vecina(net, tren["linea"], sid, tren["destino"], sentido)
             if sid is not None else None)
        slots = prog.get((sid, v)) if v is not None and (sid, v) not in dudosos else None
        if not slots:
            continue
        r = _casar_slot(slots, ts + wp["eta"])
        if r is None:
            continue
        return {"retraso_s": r[1], "estacion": nom, "hora_prog": _hhmm(r[0])}
    return None


def retraso_arribada(con, net, sid: int, linea: int, destino: str, ts_prev: int,
                     sentido: int | None = None, prog: dict | None = None,
                     dudosos: set | None = None) -> dict | None:
    """Desfase de UNA llegada anunciada en la estación `sid`: el instante
    previsto de paso (`ts_prev` = ts de la pasada + eta) frente a la hora
    programada de paso por `sid` hacia su vecina en el sentido del destino.
    {retraso_s, hora_prog} (+ tarde, − adelantado) o None si no hay horario,
    el destino no fija sentido, el par es incoherente, está a más de VENTANA
    o el casamiento es ambiguo (MARGEN_AMBIG). `prog`/`dudosos` se pueden
    pasar ya calculados para no repetir la consulta por cada llegada."""
    dia = dt.date.fromtimestamp(ts_prev).isoformat()
    if prog is None:
        prog = _prog_dia(con, dia)
    if not prog:
        return None
    if dudosos is None:
        dudosos = incoherentes(con, net, dia)
    v = _vecina(net, linea, sid, destino, sentido)
    if v is None or (sid, v) in dudosos:
        return None
    r = _casar_slot(prog.get((sid, v)) or [], ts_prev)
    if r is None:
        return None
    return {"retraso_s": r[1], "hora_prog": _hhmm(r[0])}


# -------------------------------------------------- registros del día (popups)

CASA_OBS = 150     # s: un paso observado en la estación es el del tren si dista menos de esto
DESAPAREIX = 180   # s: una estación que deja los waypoints con más ETA que esto no se ha pasado


def _fila_registro(con, net, sid: int, linea: int, destino: str, ts_paso: int,
                   sent: dict, prog: dict, dudosos: set) -> dict:
    """Una fila de registro: paso (real o previsto) frente a su hora programada."""
    r = retraso_arribada(con, net, sid, linea, destino, ts_paso,
                         sent.get((linea, destino)), prog, dudosos) if prog else None
    return {"estacion_id": sid, "estacion": net.estaciones[sid].nombre,
            "linea": linea, "destino": destino, "ts": ts_paso, "hora": _hhmm(ts_paso),
            "hora_prog": r["hora_prog"] if r else None,
            "retraso_s": r["retraso_s"] if r else None}


def registro_estacion(con, net, sid: int, dia: str) -> list[dict]:
    """Pasos reales del día por la estación `sid` (los de pasos_obs con estado
    'salida'), cada uno con su hora programada y su desfase si casa sin
    ambigüedad. Del más reciente al más antiguo."""
    t0, t1 = _rango_dia(dia)
    prog = _prog_dia(con, dia)
    dudosos = incoherentes(con, net, dia) if prog else set()
    sent = sentidos_inferidos(con, net, dia) if prog else {}
    filas = con.execute(
        "SELECT linea, destino, ts_salida FROM pasos_obs WHERE +estado='salida' "
        "AND ts_salida>=? AND ts_salida<? AND origen_id=? ORDER BY ts_salida DESC",
        (t0, t1, sid)).fetchall()
    return [_fila_registro(con, net, sid, l, d, ts, sent, prog, dudosos)
            for l, d, ts in filas]


def registro_tren(con, net, track_id: int, ahora: int | None = None) -> list[dict]:
    """Paso por estación de UN tren (track_id) en el día, frente al horario.

    Sale de su historial en `trenes`: cada pasada trae los waypoints (estaciones
    por delante con su ETA). Una estación que estuvo por delante y ya no está,
    la ha pasado: hora = la que daba la última pasada que la vio (ts + eta),
    sin pasar de la pasada siguiente; si la estación registró el paso de ese
    (línea, destino) a menos de CASA_OBS, manda ese paso observado. Las
    estaciones aún por delante salen como 'previst'. En orden de paso."""
    import bisect
    ahora = ahora or int(time.time())
    dia = dt.date.fromtimestamp(ahora).isoformat()
    t0, t1 = _rango_dia(dia)
    filas = con.execute(
        "SELECT ts, linea, destino, waypoints FROM trenes WHERE track_id=? "
        "AND ts>=? AND ts<? ORDER BY ts", (track_id, t0, t1)).fetchall()
    if not filas:
        return []
    tss = [f[0] for f in filas]
    vista: dict[tuple, tuple] = {}   # (sid, linea, destino) -> (ts, eta) de la última pasada que la vio
    for ts, linea, destino, wps in filas:
        for wp in json.loads(wps or "[]"):
            nom, eta = wp.get("nom"), wp.get("eta")
            if not nom or eta is None or eta >= HORIZONTE:
                continue
            sid = net.id_por_nombre(nom)
            if sid is not None:
                vista[(sid, linea, destino)] = (ts, eta)
    ultima = tss[-1]
    prog = _prog_dia(con, dia)
    dudosos = incoherentes(con, net, dia) if prog else set()
    sent = sentidos_inferidos(con, net, dia) if prog else {}
    out = []
    for (sid, linea, destino), (ts, eta) in vista.items():
        previst = ts == ultima
        if previst:
            paso = ts + eta
        else:
            k = bisect.bisect_right(tss, ts)
            if ts + eta > tss[k] + DESAPAREIX:
                continue          # aún le faltaba: salió de sus waypoints por otro motivo
            paso = min(ts + eta, tss[k])
            obs = con.execute(
                "SELECT ts_salida FROM pasos_obs WHERE +estado='salida' AND origen_id=? "   # +estado: índice por hora
                "AND linea=? AND destino=? AND ts_salida BETWEEN ? AND ? "
                "ORDER BY abs(ts_salida-?) LIMIT 1",
                (sid, linea, destino, paso - CASA_OBS, paso + CASA_OBS, paso)).fetchone()
            if obs:
                paso = obs[0]
        f = _fila_registro(con, net, sid, linea, destino, paso, sent, prog, dudosos)
        f["previst"] = previst
        out.append(f)
    out.sort(key=lambda f: (f["previst"], f["ts"]))
    return out


# -------------------------------------------------------- cancelados del día

MARGEN_CANC = 60       # s: un paso hasta 1 min antes de su hora sigue siendo ese tren
INTERVALO_FIN = 1200   # s: "siguiente" ficticio tras la última hora del día si no hay anterior
HORIZONTE_CANC = 1800  # s: tras la hora programada, plazo en que se cuentan trenes para confirmar el déficit
HOLGURA_CANC = 300     # s: las últimas horas del horizonte no se exigen (pueden llegar tarde)


def _lineas_del_par(net, sid: int, v: int) -> list[int]:
    """Líneas comerciales en cuyo trazado `sid` y `v` son consecutivas."""
    out = set()
    for lid, lc in net.comerciales.items():
        o = [s for _d, s in lc.orden]
        if sid in o and v in o and abs(o.index(sid) - o.index(v)) == 1:
            out.add(lc.variante_de or lid)     # una variante de ramal (41/42) es su línea (4)
    return sorted(out)


def cancelados_estacion(con, net, sid: int, dia: str, ahora: int | None = None,
                        dudosos: set | None = None) -> list[dict]:
    """Trenes programados que NO han pasado por la estación `sid` en `dia`.

    Regla (la del usuario): para cada vecina, con las horas programadas
    P_1 < P_2 < ..., la P_i está cancelada si ningún paso real por la estación
    hacia esa vecina cae en [P_i − MARGEN_CANC, P_{i+1}): el tren "no salió ni a
    su hora ni antes que la hora del siguiente". Cada paso real cuenta una sola
    vez (se casan por orden). Para la última hora del día, P_{n+1} = P_n + el
    intervalo anterior (o INTERVALO_FIN).

    El horario de paso no distingue líneas: en un tronco compartido "el
    siguiente" está a 2-3 min y cualquier retraso mayor parecería una
    cancelación (regla sola: ~900 "cancelados" en un día con 8 reales). Por eso
    la cancelación se CONFIRMA con un déficit de trenes: en [P_i − MARGEN_CANC,
    P_i + HORIZONTE_CANC) tiene que haber pasado MENOS trenes que horas
    programadas hay en [P_i, P_i + HORIZONTE_CANC − HOLGURA_CANC); si pasaron
    tantos como estaban previstos, solo iban tarde. Solo se juzga cuando
    ahora > max(P_{i+1}, P_i + HORIZONTE_CANC) + MARGEN_CANC, si la estación se
    leyó bien alrededor (sin lecturas no es cancelado: es "sin datos"), si la
    fuente publica esa dirección en esa estación (algún paso real hacia esa
    vecina en el día) y si el par no es incoherente (`incoherentes`)."""
    import bisect
    from .salidas import HUECO_MAX, _cobertura, _cubierto
    ahora = ahora or int(time.time())
    t0, t1 = _rango_dia(dia)
    prog = _prog_dia(con, dia)
    pares_est = {k: v for k, v in prog.items() if k[0] == sid}
    if not pares_est:
        return []
    if dudosos is None:
        dudosos = incoherentes(con, net, dia)
    sent = sentidos_inferidos(con, net, dia)
    reales: dict[int, list[int]] = {}
    # `+estado`: que SQLite no elija el índice por estado (recorrería todos los
    # pasos históricos) y use el de ts_salida (solo el día)
    for linea, destino, tp in con.execute(
            "SELECT linea, destino, ts_salida FROM pasos_obs WHERE origen_id=? "
            "AND +estado='salida' AND ts_salida>=? AND ts_salida<?",
            (sid, t0 - 3600, t1 + 3600)):
        v = _vecina(net, linea, sid, destino, sent.get((linea, destino)))
        if v is not None:
            reales.setdefault(v, []).append(tp)
    cob = None
    out = []
    for (_s, v), slots in pares_est.items():
        obs = sorted(reales.get(v, []))
        if not obs or (sid, v) in dudosos:
            continue                              # la fuente no publica esa dirección aquí / horario malo
        k = 0                                     # primer paso real aún sin casar
        for i, p in enumerate(slots):
            if i + 1 < len(slots):
                sig = slots[i + 1]
            else:
                sig = p + (p - slots[i - 1] if i > 0 else INTERVALO_FIN)
            ini = p - MARGEN_CANC
            while k < len(obs) and obs[k] < ini:
                k += 1                            # pasos anteriores a la ventana: no son este tren
            if k < len(obs) and obs[k] < sig:
                k += 1                            # este tren pasó (a su hora o antes del siguiente)
                continue
            fin = max(sig, p + HORIZONTE_CANC)
            if ahora <= fin + MARGEN_CANC:
                continue                          # pendiente: aún puede pasar / confirmarse
            n_prog = bisect.bisect_left(slots, p + HORIZONTE_CANC - HOLGURA_CANC) - i
            n_real = bisect.bisect_left(obs, p + HORIZONTE_CANC) - bisect.bisect_left(obs, ini)
            if n_real >= n_prog:
                continue                          # pasaron tantos como estaban previstos: solo retraso
            if cob is None:
                cob = _cobertura(con, sid, t0 - HUECO_MAX, t1 + HUECO_MAX)
            if not (_cubierto(cob, p) and _cubierto(cob, sig)):
                continue                          # sin lecturas: no se sabe, no es cancelado
            out.append({"hora": _hhmm(p), "ts_prog": p, "hacia_id": v,
                        "hacia": net.estaciones[v].nombre if v in net.estaciones else str(v),
                        "lineas": _lineas_del_par(net, sid, v)})
    out.sort(key=lambda f: (f["ts_prog"], f["hacia_id"]))
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
    ap.add_argument("--dedupe", action="store_true",
                    help="una vez: horario de paso por tipo de día (poller en marcha vale)")
    args = ap.parse_args()
    net = load_network()
    if args.dedupe:
        con = sqlite3.connect(DB, timeout=120)
        print(dedupe_todo(con, net))
        return
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

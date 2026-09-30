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
import threading
import time

from . import horarios
from .salidas import _rango_dia, seguir

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

def _vecina(net, linea: int, sid: int, destino: str,
            sentido: int | None = None) -> int | None:
    """Estación siguiente a `sid` en la línea, en el sentido del destino. Si el
    destino no está en el trazado de la línea (p. ej. L1 rotulada "Benimaclet"
    que en realidad va hacia Moncada), usa `sentido` (+1 = orden creciente)."""
    lc = net.comerciales.get(linea)
    if not lc:
        return None
    orden = [s for _d, s in lc.orden]
    if sid not in orden:
        return None
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
            "WHERE estado='salida' AND ts_salida>=? AND ts_salida<?", (t0 - 86400, t1)):
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
    for s, v, tp in con.execute(
            "SELECT estacion_id, hacia_id, ts_prog FROM horario_paso WHERE dia=?", (dia,)):
        prog.setdefault((s, v), []).append(tp)
    sent = sentidos_inferidos(con, net, dia)
    grupos: dict[tuple[int, int], list[tuple]] = {}
    for sid, linea, destino, tpaso in con.execute(
            "SELECT origen_id, linea, destino, ts_salida FROM pasos_obs "
            "WHERE estado='salida' AND ts_salida>=? AND ts_salida<?", (t0, t1)):
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
        for s, v, tp in con.execute(
                "SELECT estacion_id, hacia_id, ts_prog FROM horario_paso WHERE dia=?", (dia,)):
            prog.setdefault((s, v), []).append(tp)
        for v in prog.values():
            v.sort()
        if prog:                      # no cachear un día aún sin descargar
            _prog_cache.clear()
            _prog_cache[dia] = prog
        return prog
    return _prog_cache[dia]


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
        real = ts + wp["eta"]
        cands = sorted(slots, key=lambda p: abs(real - p))
        d = real - cands[0]
        if abs(d) > VENTANA:
            continue
        if len(cands) > 1 and abs(real - cands[1]) < abs(d) + MARGEN_AMBIG:
            continue                  # tronco compartido: no se sabe cuál es
        return {"retraso_s": d, "estacion": nom,
                "hora_prog": dt.datetime.fromtimestamp(cands[0]).strftime("%H:%M")}
    return None


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

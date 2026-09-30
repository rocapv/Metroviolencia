"""
Relleno del registro hacia atrás con el histórico, desde el 08/07/2026: del
03 al 07/07 (días de montaje del proyecto) los datos no son fiables (hasta
4 000 trenes/día que aparecen una vez y se esfuman).

Fases (reanudables; cada una salta lo ya hecho):

  A · replay   Reproduce TODAS las pasadas antiguas (data/archivo/*.csv.gz +
               lo que queda en `arrivals`) con el mismo seguimiento que el
               poller, en una base TEMPORAL (data/tmp/backfill.db) para no
               pisar las entradas abiertas del poller en vivo. Guarda también
               los paneles vacíos de cabecera y los destinos vistos por semana.
  B · horario  Horario de salida desde cabecera de cada fecha (planificador),
               con los destinos que circulaban ESA semana.
  C · paso     Horario de paso por estación (290 pares/día). Mismo tipo de día
               (color del calendario) = mismo horario: se descarga un día por
               grupo (tipos metro/tranvía/L10 + periodo) y se reutiliza para
               los demás SOLO si su horario de cabecera coincide exactamente.
  D · copia    Copia a metro.db las salidas/pasos/lecturas anteriores a lo que
               ya registraba el poller.

    python -m backend.backfill [--fase A|B|C|D|todo]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import gzip
import sqlite3
import time
from collections import Counter
from pathlib import Path

from . import horarios, pasos, salidas
from .cabeceras import CABECERAS, MIN_VISTAS
from .calendario import tipo
from .network import load_network

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
TMP = ROOT / "data" / "tmp" / "backfill.db"
ARCHIVO = ROOT / "data" / "archivo"
PERDIDOS = {23384, 23385}          # snapshots purgados sin archivar (15/09)
FIN_CIERRE = "2026-09-01"          # corte Alameda–Marítim: cambia el horario

TMP_SCHEMA = """
CREATE TABLE IF NOT EXISTS dest_semana (
    origen_id INTEGER, semana TEXT, linea INTEGER, destino TEXT, n INTEGER,
    PRIMARY KEY (origen_id, semana, linea, destino));
CREATE TABLE IF NOT EXISTS vacias (snapshot_id INTEGER, ts INTEGER, estacion_id INTEGER);
CREATE TABLE IF NOT EXISTS estado (clave TEXT PRIMARY KEY, valor TEXT);
"""


def _semana(ts: int) -> str:
    y, w, _ = dt.date.fromtimestamp(ts).isocalendar()
    return f"{y}-W{w:02d}"


def _fuente(main: sqlite3.Connection):
    """Genera (snapshot_id, [(estacion, linea, destino, eta)]) en orden, del
    archivo CSV y luego de `arrivals`."""
    ult = 0
    for f in sorted(glob.glob(str(ARCHIVO / "arrivals_*.csv.gz"))):
        with gzip.open(f, "rt", newline="") as fh:
            r = csv.reader(fh)
            next(r)
            actual, filas = None, []
            for sid, _ts, est, lin, dest, eta, _txt in r:
                sid = int(sid)
                if sid != actual:
                    if actual is not None:
                        yield actual, filas
                    actual, filas = sid, []
                filas.append((int(est), int(lin), dest, int(eta)))
            if actual is not None:
                yield actual, filas
                ult = actual
    actual, filas = None, []
    for sid, est, lin, dest, eta in main.execute(
            "SELECT snapshot_id, estacion_id, linea, destino, eta_seg FROM arrivals "
            "WHERE snapshot_id>? ORDER BY snapshot_id", (ult,)):
        if sid != actual:
            if actual is not None:
                yield actual, filas
            actual, filas = sid, []
        filas.append((est, lin, dest, eta))
    if actual is not None:
        yield actual, filas


def fase_a(main, tmp, net, desde_ts: int, hasta_ts: int, log=print) -> None:
    if tmp.execute("SELECT valor FROM estado WHERE clave='A'").fetchone():
        log("A: ya hecha")
        return
    for t in ("salidas_obs", "pasos_obs", "dest_semana", "vacias"):
        tmp.execute(f"DELETE FROM {t}")
    snaps = main.execute("SELECT id, ts FROM snapshots WHERE ts>=? AND ts<? ORDER BY id",
                         (desde_ts, hasta_ts)).fetchall()
    fuente = _fuente(main)
    pend = next(fuente, None)
    todas = list(net.estaciones)
    cab_ids = {c["id"]: c for c in CABECERAS}
    dest = Counter()
    t0 = time.time()
    for k, (snap_id, ts) in enumerate(snaps):
        while pend and pend[0] < snap_id:
            pend = next(fuente, None)
        filas = pend[1] if pend and pend[0] == snap_id else []
        if not filas and snap_id in PERDIDOS:
            continue                      # sin lectura: no se envejece nada
        por = {s: [] for s in todas}      # panel vacío = leído sin trenes
        for est, lin, de, eta in filas:
            if est in por:
                por[est].append((lin, de, eta))
        salidas.seguir(tmp, "salidas_obs", ts, CABECERAS, por)
        pasos.actualizar(tmp, ts, por, net)
        tmp.executemany("INSERT INTO vacias VALUES (?,?,?)",
                        [(snap_id, ts, c) for c in cab_ids if not por[c]])
        sem = _semana(ts)
        for c, cab in cab_ids.items():
            for lin, de, eta in por[c]:
                if lin in cab["lineas"] and de and de != cab["nombre"] and eta < 3600:
                    dest[(c, sem, lin, de)] += 1
        if k % 2000 == 0:
            log(f"A: {k}/{len(snaps)} pasadas ({time.time() - t0:.0f} s)")
    tmp.executemany("INSERT OR REPLACE INTO dest_semana VALUES (?,?,?,?,?)",
                    [(*kk, n) for kk, n in dest.items()])
    tmp.execute("INSERT OR REPLACE INTO estado VALUES ('A', ?)", (str(int(time.time())),))
    tmp.commit()
    log(f"A: hecha, {len(snaps)} pasadas en {time.time() - t0:.0f} s")


def _dias(desde: str, hasta: str) -> list[str]:
    d, h = dt.date.fromisoformat(desde), dt.date.fromisoformat(hasta)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range((h - d).days + 1)]


def _destinos_semana(tmp, dia: str):
    """destinos(cab) con lo visto la semana del día y la anterior."""
    d = dt.date.fromisoformat(dia)
    sems = {_semana(int(time.mktime(x.timetuple()))) for x in (d, d - dt.timedelta(days=7))}

    def f(cab):
        cnt = Counter()
        for sem in sems:
            for lin, de, n in tmp.execute(
                    "SELECT linea, destino, n FROM dest_semana WHERE origen_id=? AND semana=?",
                    (cab["id"], sem)):
                cnt[(de, lin)] += n
        return [(de, lin, n) for (de, lin), n in cnt.items() if n >= MIN_VISTAS]
    return f


def fase_b(main, tmp, net, dias: list[str], log=print) -> None:
    from .poller import HEADERS, VERIFY, make_session
    s = make_session()
    for dia in dias:
        n = horarios.descargar_dia(main, s, HEADERS, VERIFY, dia, net,
                                   log=lambda *_: None, destinos=_destinos_semana(tmp, dia))
        log(f"B: {dia} {n} salidas programadas")


def _firma(dia: str) -> tuple:
    return (tipo(dia, 1), tipo(dia, 4), tipo(dia, 10), dia >= FIN_CIERRE)


def _horario_cab(main, dia: str) -> dict:
    """Horario de cabecera del día para comparar tipos. Se ignora la franja
    23:30–00:30: ahí el planificador baila 1-2 trenes de un día a otro por el
    límite de medianoche aunque el horario sea el mismo."""
    out: dict[int, list[str]] = {}
    for o, h in main.execute("SELECT origen_id, hora FROM horario_prog WHERE dia=? "
                             "ORDER BY origen_id, hora", (dia,)):
        if "00:30" <= h < "23:30":
            out.setdefault(o, []).append(h)
    return out


def fase_c(main, net, dias: list[str], log=print) -> None:
    from .poller import HEADERS, VERIFY, make_session
    s = make_session()
    pasos.init_db(main)
    # TODOS los días descargados de cada tipo: el horario cambia a veces a mitad
    # de temporada (p. ej. 20/07) y hay que poder casar con el más reciente
    reps: dict[tuple, list[str]] = {}
    # representantes ya descargados completos (p. ej. días recientes)
    completos = {r[0] for r in main.execute(
        "SELECT dia FROM horario_paso_desc GROUP BY dia HAVING count(*)>=?",
        (len(pasos.pares(net)),))}
    for dia in dias:
        if dia in completos:
            reps.setdefault(_firma(dia), []).append(dia)
            continue
        f = _firma(dia)
        hc = _horario_cab(main, dia)
        rep = next((r for r in reversed(reps.get(f, [])) if _horario_cab(main, r) == hc), None)
        if rep:
            filas = main.execute("SELECT estacion_id, hacia_id, hora FROM horario_paso "
                                 "WHERE dia=?", (rep,)).fetchall()
            main.executemany(
                "INSERT OR IGNORE INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                " VALUES (?,?,?,?,?)", [(dia, a, b, horarios._ts(dia, h), h) for a, b, h in filas])
            main.execute("INSERT OR REPLACE INTO horario_paso_desc(dia,estacion_id,hacia_id,n) "
                         "SELECT ?, estacion_id, hacia_id, n FROM horario_paso_desc WHERE dia=?",
                         (dia, rep))
            main.commit()
            reps[f].append(dia)
            log(f"C: {dia} copiado de {rep} (mismo tipo {f[:3]})")
            continue
        previo = (reps.get(f) or [None])[-1]
        n = pasos.descargar_dia(main, s, HEADERS, VERIFY, dia, net, log=lambda *_: None)
        reps.setdefault(f, []).append(dia)
        log(f"C: {dia} descargado ({n} horas)"
            f"{' — distinto de ' + previo if previo else ' — nuevo tipo ' + str(f[:3])}")


def fase_d(main, tmp, log=print) -> None:
    for tabla in ("salidas_obs", "pasos_obs"):
        lim = main.execute(f"SELECT min(ts_primera) FROM {tabla}").fetchone()[0] or 2**31
        filas = tmp.execute(
            f"SELECT origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,ts_pred,"
            f"n_vistas,estado,ts_salida FROM {tabla} WHERE ts_primera<?", (lim,)).fetchall()
        # lo que quedó abierto al final del replay lo sigue el poller: fuera
        filas = [f for f in filas if f[8] != "en_espera"]
        main.executemany(
            f"INSERT INTO {tabla}(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
            f"ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,?,?)", filas)
        main.commit()
        log(f"D: {tabla} +{len(filas)} filas (anteriores a {dt.datetime.fromtimestamp(lim)})")
    lim = main.execute("SELECT min(snapshot_id) FROM lecturas_fallidas").fetchone()[0] or 2**31
    filas = tmp.execute("SELECT snapshot_id, ts, estacion_id FROM vacias WHERE snapshot_id<?",
                        (lim,)).fetchall()
    main.executemany("INSERT INTO lecturas_fallidas(snapshot_id,ts,estacion_id,motivo) "
                     "VALUES (?,?,?,'vacia')", filas)
    main.commit()
    log(f"D: lecturas_fallidas +{len(filas)} (paneles vacíos de cabecera)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fase", default="todo", choices=["A", "B", "C", "D", "todo"])
    ap.add_argument("--desde", default="2026-07-08")
    args = ap.parse_args()
    log = lambda *a: print(time.strftime("[%H:%M:%S]"), *a, flush=True)  # noqa: E731
    net = load_network()
    main_con = sqlite3.connect(DB, timeout=120)
    TMP.parent.mkdir(exist_ok=True)
    tmp = sqlite3.connect(TMP)
    tmp.execute("PRAGMA synchronous=OFF")
    tmp.execute("PRAGMA journal_mode=MEMORY")
    salidas.init_db(tmp)
    pasos.init_db(tmp)
    tmp.executescript(TMP_SCHEMA)
    # hasta el primer día que ya registraba el poller con pasos (el más tardío)
    lim_pasos = main_con.execute("SELECT min(ts_primera) FROM pasos_obs").fetchone()[0]
    hasta_ts = lim_pasos or int(time.time())
    dia0 = args.desde
    # una hora antes del primer día, para que el seguimiento arranque "caliente"
    desde_ts = horarios._ts(dia0, "00:00") - 3600
    dia_fin = (dt.date.fromtimestamp(hasta_ts)).isoformat()
    dias = _dias(dia0, dia_fin)
    log(f"relleno {dia0} .. {dia_fin} ({len(dias)} días)")
    if args.fase in ("A", "todo"):
        fase_a(main_con, tmp, net, desde_ts, hasta_ts, log)
    if args.fase in ("B", "todo"):
        fase_b(main_con, tmp, net, dias, log)
    if args.fase in ("C", "todo"):
        fase_c(main_con, net, dias, log)
    if args.fase in ("D", "todo"):
        fase_d(main_con, tmp, log)
    log("fin")


if __name__ == "__main__":
    main()

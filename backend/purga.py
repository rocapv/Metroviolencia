"""
Purga de datos brutos de metro.db para que no se coma la tarjeta SD.

A pull de 1 min la base crecía ~135 MB/día, casi todo en `arrivals` (llegadas
crudas de cada pasada) y `trenes` (posiciones estimadas). Lo que vale a largo
plazo es pequeño y se conserva SIEMPRE: snapshots, horarios, salidas_obs,
lecturas_fallidas, noinfo, y el RESUMEN de retrasos de cada día.

    arrivals   -> RETENER_ARRIVALS días (permite --reprocesar lo reciente)
    trenes     -> RETENER_TRENES días (solo sirven para el mapa en vivo)
    pasos_obs  -> RETENER_PASOS días; antes de borrar un día se guarda su
                  resumen de retrasos por línea y por parada (pasos.guardar_resumen),
                  que es lo que sirve /api/retards_ruta para días antiguos.
                  (2,2 M filas en 85 días: era la tabla que más crecía.)

Se borra por tandas de snapshots (bloqueos cortos: el poller sigue) y el espacio
se devuelve al disco con incremental_vacuum (requiere auto_vacuum=INCREMENTAL,
que se activa UNA vez con --vacuum, con el poller y la web PARADOS).

    python -m backend.purga             # purga (la lanza el poller cada día)
    python -m backend.purga --vacuum    # una vez: auto_vacuum + VACUUM
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import time
from pathlib import Path

from . import pasos

RETENER_ARRIVALS = 14   # días
RETENER_TRENES = 3      # días
RETENER_PASOS = 30      # días de pasos crudos; los anteriores quedan resumidos
MAX_RESUMENES = 10      # días resumidos por pasada (~6 s cada uno en la Raspa)
TANDA = 30              # snapshots por transacción
PAGINAS_VACUUM = 20000  # páginas (4 KB) devueltas al disco por pasada de purga

TMPDIR = Path(__file__).resolve().parent.parent / "data" / "tmp"


def _corte(con, dias: int) -> int | None:
    r = con.execute("SELECT max(id) FROM snapshots WHERE ts<?",
                    (int(time.time()) - dias * 86400,)).fetchone()
    return r[0] if r and r[0] is not None else None


def _borrar(con, tabla: str, hasta_snap: int, log) -> int:
    r = con.execute(f"SELECT min(snapshot_id) FROM {tabla}").fetchone()
    if not r or r[0] is None or r[0] > hasta_snap:
        return 0
    total = 0
    desde = r[0]
    while desde <= hasta_snap:
        fin = min(desde + TANDA - 1, hasta_snap)
        cur = con.execute(f"DELETE FROM {tabla} WHERE snapshot_id BETWEEN ? AND ?",
                          (desde, fin))
        con.commit()
        total += cur.rowcount
        desde = fin + 1
        time.sleep(0.05)   # deja pasar al poller entre tandas
    log(f"  purga {tabla}: {total} filas (snapshots <= {hasta_snap})")
    return total


def _resumir_y_purgar_pasos(con, log) -> dict:
    """Resume (por línea y parada) los días de pasos más antiguos que
    RETENER_PASOS que aún no lo estén, y borra los pasos crudos de los días
    resumidos. Se deja sin borrar el día anterior al primero no resumido:
    sentidos_inferidos() mira también el día previo."""
    from .network import load_network
    from .salidas import _rango_dia
    pasos.init_db(con)
    r = con.execute("SELECT min(ts_salida) FROM pasos_obs WHERE estado='salida'").fetchone()
    if not r or r[0] is None:
        return {"resumidos": 0, "borrados": 0}
    primero = dt.date.fromtimestamp(r[0])
    corte = dt.date.today() - dt.timedelta(days=RETENER_PASOS)   # se resumen días < corte
    hechos = {x[0] for x in con.execute("SELECT dia FROM retards_resumits")}
    pendientes = []
    d = primero
    while d < corte:
        if d.isoformat() not in hechos:
            pendientes.append(d.isoformat())
        d += dt.timedelta(days=1)
    net = load_network() if pendientes else None
    n_res = 0
    for dia in pendientes[:MAX_RESUMENES]:
        n = pasos.guardar_resumen(con, net, dia)
        hechos.add(dia)
        n_res += 1
        log(f"  resumen {dia}: {n} pasos casados")
        time.sleep(0.2)
    # borrar hasta el día anterior al primer día no resumido (exclusive)
    d = primero
    while d < corte and d.isoformat() in hechos:
        d += dt.timedelta(days=1)
    limite = min(d, corte) - dt.timedelta(days=1)
    borrados = 0
    if limite > primero:
        t_lim = _rango_dia(limite.isoformat())[0]
        while True:
            cur = con.execute("DELETE FROM pasos_obs WHERE rowid IN (SELECT rowid FROM pasos_obs "
                              "WHERE estado!='en_espera' AND ts_ultima<? LIMIT 5000)", (t_lim,))
            con.commit()
            borrados += cur.rowcount
            if cur.rowcount < 5000:
                break
            time.sleep(0.05)   # deja pasar al poller entre tandas
        if borrados:
            log(f"  purga pasos_obs: {borrados} filas (anteriores a {limite})")
    return {"resumidos": n_res, "pendientes": max(0, len(pendientes) - n_res), "borrados": borrados}


def purgar(con: sqlite3.Connection, log=print) -> dict:
    res = {}
    c = _corte(con, RETENER_ARRIVALS)
    res["arrivals"] = _borrar(con, "arrivals", c, log) if c else 0
    c = _corte(con, RETENER_TRENES)
    res["trenes"] = _borrar(con, "trenes", c, log) if c else 0
    try:
        res["pasos_obs"] = _resumir_y_purgar_pasos(con, log)
    except Exception as e:  # noqa: BLE001 - que no tumbe el resto de la purga
        log(f"  purga pasos_obs: error {e}")
    if con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:     # INCREMENTAL
        libres = con.execute("PRAGMA freelist_count").fetchone()[0]
        con.execute(f"PRAGMA incremental_vacuum({min(libres, PAGINAS_VACUUM)})")
        con.commit()
        res["paginas_devueltas"] = min(libres, PAGINAS_VACUUM)
    return res


def vacuum(db: Path) -> None:
    """Activa auto_vacuum=INCREMENTAL y compacta. VACUUM reescribe la base
    entera: el temporal va a data/tmp (en disco) porque /tmp es tmpfs (RAM)."""
    TMPDIR.mkdir(exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = str(TMPDIR)
    con = sqlite3.connect(db, timeout=120)
    con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    con.execute("VACUUM")
    con.close()


def main():
    from .poller import DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--vacuum", action="store_true",
                    help="una vez, con poller y web PARADOS: auto_vacuum + VACUUM")
    args = ap.parse_args()
    t = time.time()
    con = sqlite3.connect(DB, timeout=120)
    print(purgar(con))
    con.close()
    if args.vacuum:
        vacuum(DB)
    print(f"hecho en {time.time() - t:.0f} s; tamaño {DB.stat().st_size / 2**30:.2f} GB")


if __name__ == "__main__":
    main()

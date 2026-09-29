"""
Purga de datos brutos de metro.db para que no se coma la tarjeta SD.

A pull de 1 min la base crecía ~135 MB/día, casi todo en `arrivals` (llegadas
crudas de cada pasada) y `trenes` (posiciones estimadas). Lo que vale a largo
plazo es pequeño y se conserva SIEMPRE: snapshots, horarios, salidas_obs,
pasos_obs, lecturas_fallidas, noinfo.

    arrivals   -> RETENER_ARRIVALS días (permite --reprocesar lo reciente)
    trenes     -> RETENER_TRENES días (solo sirven para el mapa en vivo)

Se borra por tandas de snapshots (bloqueos cortos: el poller sigue) y el espacio
se devuelve al disco con incremental_vacuum (requiere auto_vacuum=INCREMENTAL,
que se activa UNA vez con --vacuum, con el poller y la web PARADOS).

    python -m backend.purga             # purga (la lanza el poller cada día)
    python -m backend.purga --vacuum    # una vez: auto_vacuum + VACUUM
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

RETENER_ARRIVALS = 14   # días
RETENER_TRENES = 3      # días
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


def purgar(con: sqlite3.Connection, log=print) -> dict:
    res = {}
    c = _corte(con, RETENER_ARRIVALS)
    res["arrivals"] = _borrar(con, "arrivals", c, log) if c else 0
    c = _corte(con, RETENER_TRENES)
    res["trenes"] = _borrar(con, "trenes", c, log) if c else 0
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

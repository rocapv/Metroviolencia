"""
Lectures de FGV, compactes: UNA fila per pasada.

Fins al 04/11/2026 cada arribada llegida era una fila d'`arrivals` (~1.300 per
pasada, amb dos índexs): 990 MB dels 1,7 GB de metro.db, i 1.300 insercions a
la SD cada 2 minuts. Ningú no les consulta soltes: el directe vol les de
l'última pasada, i el reprocés, totes les d'unes pasades seguides. Ara cada
pasada és un blob zlib(JSON) de ~8 KB:

    lectures(snapshot_id PK, ts, dades)    dades = [[estacio, linia, desti, eta_seg, eta_txt, ocupacio], ...]

El recompte de destins vistos a cada capçalera (cabeceras.destinos_de, 7
dies) ja no pot fer un GROUP BY sobre les files: el poller el va sumant a
`destins_capcalera` (dia, estació, línia, destí, n).

    python -m backend.lectures --migra   # una vegada: arrivals -> lectures (poller PARAT)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
import zlib

DDL = """
CREATE TABLE IF NOT EXISTS lectures (
    snapshot_id INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    dades       BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lectures_ts ON lectures(ts);

CREATE TABLE IF NOT EXISTS destins_capcalera (
    dia         TEXT    NOT NULL,
    estacion_id INTEGER NOT NULL,
    linea       INTEGER NOT NULL,
    destino     TEXT    NOT NULL,
    n           INTEGER NOT NULL,
    PRIMARY KEY (estacion_id, dia, linea, destino)
) WITHOUT ROWID;
"""


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(DDL)


def empaqueta(files: list) -> bytes:
    """files: [(estacio, linia, desti, eta_seg, eta_txt, ocupacio)]"""
    return zlib.compress(json.dumps([list(f) for f in files], ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8"), 6)


def desempaqueta(blob: bytes) -> list[tuple]:
    return [tuple(f) for f in json.loads(zlib.decompress(blob).decode("utf-8"))]


def guarda(con: sqlite3.Connection, snap_id: int, ts: int, files: list) -> None:
    """Guarda les arribades d'una pasada i suma els destins vistos a les capçaleres."""
    from .cabeceras import POR_ID
    con.execute("INSERT OR REPLACE INTO lectures(snapshot_id, ts, dades) VALUES (?,?,?)",
                (snap_id, ts, empaqueta(files)))
    dia = dt.date.fromtimestamp(ts).isoformat()
    vistos: dict[tuple, int] = {}
    for est, lin, dest, *_ in files:
        if est in POR_ID and dest:
            vistos[(est, lin, dest)] = vistos.get((est, lin, dest), 0) + 1
    con.executemany(
        "INSERT INTO destins_capcalera(dia, estacion_id, linea, destino, n) VALUES (?,?,?,?,?) "
        "ON CONFLICT(estacion_id, dia, linea, destino) DO UPDATE SET n = n + excluded.n",
        [(dia, e, l, d, n) for (e, l, d), n in vistos.items()])


def de_pasada(con: sqlite3.Connection, snap_id: int) -> list[tuple]:
    """Arribades d'una pasada, ordenades per (estació, eta)."""
    r = con.execute("SELECT dades FROM lectures WHERE snapshot_id=?", (snap_id,)).fetchone()
    if not r:
        return []
    return sorted(desempaqueta(r[0]), key=lambda f: (f[0], f[3]))


def pasades(con: sqlite3.Connection, desde_ts: int | None = None, desde_snap: int | None = None):
    """(snapshot_id, ts, files) de les pasades guardades, en ordre."""
    if desde_snap is not None:
        cur = con.execute("SELECT snapshot_id, ts, dades FROM lectures WHERE snapshot_id>? "
                          "ORDER BY snapshot_id", (desde_snap,))
    else:
        cur = con.execute("SELECT snapshot_id, ts, dades FROM lectures WHERE ts>=? "
                          "ORDER BY snapshot_id", (desde_ts or 0,))
    for sid, ts, blob in cur:
        yield sid, ts, desempaqueta(blob)


def purga(con: sqlite3.Connection, dies: int, log=print) -> int:
    lim = int(time.time()) - dies * 86400
    n = con.execute("DELETE FROM lectures WHERE ts<?", (lim,)).rowcount
    corte = (dt.date.today() - dt.timedelta(days=dies)).isoformat()
    con.execute("DELETE FROM destins_capcalera WHERE dia<?", (corte,))
    con.commit()
    if n:
        log(f"  purga lectures: {n} pasades")
    return n


def migra(con: sqlite3.Connection, dies: int, log=print) -> dict:
    """arrivals (files) -> lectures (un blob per pasada), només els últims
    `dies`; després esborra la taula vella. Es pot fer amb el poller NOU en
    marxa (ja no escriu a arrivals): les tandes són curtes perquè no l'aturen."""
    init_db(con)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='arrivals'").fetchone():
        return {"migrades": 0, "nota": "ja no hi ha arrivals"}
    lim = int(time.time()) - dies * 86400
    snaps = con.execute("SELECT id, ts FROM snapshots WHERE ts>=? ORDER BY id", (lim,)).fetchall()
    if not snaps:
        return {"migrades": 0, "nota": "cap pasada en la finestra"}
    s0 = snaps[0][0]
    s1 = con.execute("SELECT max(snapshot_id) FROM arrivals").fetchone()[0] or 0
    fets = {r[0] for r in con.execute("SELECT snapshot_id FROM lectures")}
    n = files_tot = 0
    t = time.time()
    for sid, ts in reversed(snaps):      # les més noves primer: el directe les té de seguida
        if sid in fets or sid > s1:
            continue
        files = con.execute("SELECT estacion_id, linea, destino, eta_seg, eta_txt, ocupacion "
                            "FROM arrivals WHERE snapshot_id=?", (sid,)).fetchall()
        if not files:
            continue
        guarda(con, sid, ts, files)
        n += 1
        files_tot += len(files)
        if n % 25 == 0:
            con.commit()
            time.sleep(0.05)      # deixa passar el poller i la web
        if n % 500 == 0:
            log(f"  {n} pasades ({files_tot} arribades) en {time.time() - t:.0f} s")
    con.commit()
    # comprovació: les mateixes arribades a les dues bandes, en el tram migrat
    a = con.execute("SELECT count(*) FROM arrivals WHERE snapshot_id BETWEEN ? AND ?",
                    (s0, s1)).fetchone()[0]
    b = sum(len(desempaqueta(r[0])) for r in con.execute(
        "SELECT dades FROM lectures WHERE snapshot_id BETWEEN ? AND ?", (s0, s1)))
    if a != b:
        raise RuntimeError(f"no quadra: arrivals {a} != lectures {b}; NO s'esborra res")
    # amb secure_delete (el de Debian), esborrar 990 MB els reescrivia amb zeros
    # passant pel journal: >20 min de SD saturada el 04/11/2026. Sense, és marcar pàgines.
    con.execute("PRAGMA secure_delete=OFF")
    con.execute("DROP TABLE arrivals")
    con.commit()
    return {"migrades": n, "arribades": b, "segons": round(time.time() - t)}


def main():
    from .poller import DB
    from .purga import RETENER_LECTURES
    ap = argparse.ArgumentParser()
    ap.add_argument("--migra", action="store_true", help="arrivals -> lectures (poller PARAT)")
    args = ap.parse_args()
    con = sqlite3.connect(DB, timeout=120)
    if args.migra:
        print(migra(con, RETENER_LECTURES))
    con.close()


if __name__ == "__main__":
    main()

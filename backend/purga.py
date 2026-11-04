"""
Purga de datos brutos de metro.db para que no se coma la tarjeta SD.

A pull de 1 min la base crecía ~135 MB/día, casi todo en `arrivals` (llegadas
crudas de cada pasada) y `trenes` (posiciones estimadas). Lo que vale a largo
plazo es pequeño y se conserva SIEMPRE: snapshots, horarios, salidas_obs,
lecturas_fallidas, noinfo, y el RESUMEN de retrasos de cada día.

    04/11/2026: TODO lo bruto se queda en 7 días (petición de RocaPV: los
    "logs" de más de una semana fuera). `arrivals` (990 MB) pasó a `lectures`,
    un blob por pasada (ver lectures.py).

    lectures   -> RETENER_LECTURES días (permite --reprocesar lo reciente)
    trenes     -> RETENER_TRENES días (mapa, simulación y recorrido de un tren),
                  y con ellos finals_tren (cómo acabó cada tren perdido)
    lecturas_fallidas / noinfo -> RETENER_LOGS días, pero nunca de un día aún
                  sin resumir (el resumen de retrasos los usa)
    data/archivo/*.csv.gz -> fuera a los RETENER_LOGS días
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

from . import pasos, xat

RETENER_LECTURES = 7    # días
RETENER_TRENES = 7      # días
RETENER_LOGS = 7        # días (lecturas_fallidas, noinfo, data/archivo)
RETENER_PASOS = 7       # días de pasos crudos; los anteriores quedan resumidos
MAX_RESUMENES = 10      # días resumidos por pasada (~6 s cada uno en la Raspa)
TANDA = 30              # snapshots por transacción
PAGINAS_VACUUM = 20000  # páginas (4 KB) devueltas al disco por pasada de purga

TMPDIR = Path(__file__).resolve().parent.parent / "data" / "tmp"
ARCHIVO = Path(__file__).resolve().parent.parent / "data" / "archivo"


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


def _corte_logs(con) -> int | None:
    """Último snapshot borrable de lecturas_fallidas/noinfo: más viejo que
    RETENER_LOGS días y anterior al primer día con pasos sin resumir."""
    c = _corte(con, RETENER_LOGS)
    r = con.execute("SELECT min(ts_salida) FROM pasos_obs WHERE estado='salida'").fetchone()
    if c is None or not r or r[0] is None:
        return c
    inicio = int(time.mktime(dt.date.fromtimestamp(r[0] - 86400).timetuple()))   # y el día previo
    r = con.execute("SELECT max(id) FROM snapshots WHERE ts<?", (inicio,)).fetchone()
    return min(c, r[0]) if r and r[0] is not None else None


def _purgar_archivo(log) -> int:
    n = 0
    lim = time.time() - RETENER_LOGS * 86400
    for f in ARCHIVO.glob("*.csv.gz") if ARCHIVO.exists() else []:
        if f.stat().st_mtime < lim:
            f.unlink()
            n += 1
            log(f"  purga archivo: {f.name}")
    return n


def purgar(con: sqlite3.Connection, log=print) -> dict:
    from . import lectures
    con.execute("PRAGMA secure_delete=OFF")   # ver poller.init_db
    res = {}
    res["lectures"] = lectures.purga(con, RETENER_LECTURES, log)
    # (la tabla vieja `arrivals`, si queda, no se purga fila a fila: la borra
    # entera `compacta` de madrugada, que sin secure_delete es marcar páginas)
    c = _corte(con, RETENER_TRENES)
    res["trenes"] = _borrar(con, "trenes", c, log) if c else 0
    try:   # veredictes dels trens perduts (finals.py): viuen el mateix que `trenes`
        from . import finals
        finals.init_db(con)
        res["finals_tren"] = finals.purga(con, int(time.time()) - RETENER_TRENES * 86400)
    except sqlite3.OperationalError as e:
        log(f"  purga finals_tren: error {e}")
    try:
        res["pasos_obs"] = _resumir_y_purgar_pasos(con, log)
    except Exception as e:  # noqa: BLE001 - que no tumbe el resto de la purga
        log(f"  purga pasos_obs: error {e}")
    c = _corte_logs(con)
    if c:
        res["lecturas_fallidas"] = _borrar(con, "lecturas_fallidas", c, log)
        res["noinfo"] = _borrar(con, "noinfo", c, log)
    try:
        res["archivo"] = _purgar_archivo(log)
    except OSError as e:
        log(f"  purga archivo: error {e}")
    try:
        res["xat"] = xat.purgar(con)      # mensajes de más de RETENER_DIES días
    except Exception as e:  # noqa: BLE001
        log(f"  purga xat: error {e}")
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


COMPACTA_FRACCIO = 0.25   # VACUUM si més d'una quarta part del fitxer són pàgines lliures
COMPACTA_HORES = (1, 5)   # només de matinada (metro tancat; el poller passa cada 10 min)


def compacta(db: Path, log=print, ara: float | None = None) -> bool:
    """VACUUM quan sobra molt d'espai (després d'una purga gran o de la
    migració a `lectures`). La llança cada matinada deploy/metroviolencia-
    compacta.timer (Persistent=true: si la Raspa estava apagada, en arrancar;
    fora de les hores de COMPACTA_HORES no fa res i ho deixa per a la nit
    següent). En WAL la web continua llegint; el poller espera o perd una
    pasada. Idempotent: després del VACUUM ja no sobra espai."""
    hora = time.localtime(ara or time.time()).tm_hour
    if not COMPACTA_HORES[0] <= hora <= COMPACTA_HORES[1]:
        log(f"compacta: fora d'hora ({hora} h), ho deixe per a la matinada")
        return False
    con = sqlite3.connect(db, timeout=300)
    con.execute("PRAGMA secure_delete=OFF")
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='arrivals'").fetchone():
        from . import lectures   # la taula vella de la migració del 04/11/2026
        log(f"compacta: {lectures.migra(con, RETENER_LECTURES, log)}")
    pag = con.execute("PRAGMA page_count").fetchone()[0]
    lli = con.execute("PRAGMA freelist_count").fetchone()[0]
    if not pag or lli / pag < COMPACTA_FRACCIO:
        log(f"compacta: res a fer ({lli} de {pag} pàgines lliures)")
        con.close()
        return False
    TMPDIR.mkdir(exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = str(TMPDIR)   # el temporal, a disc: /tmp és RAM
    t = time.time()
    abans = db.stat().st_size
    con.execute("VACUUM")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    log(f"compacta: VACUUM en {time.time() - t:.0f} s, {abans / 2**30:.2f} -> "
        f"{db.stat().st_size / 2**30:.2f} GB")
    return True


def main():
    from .poller import DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--vacuum", action="store_true",
                    help="una vez, con poller y web PARADOS: auto_vacuum + VACUUM")
    ap.add_argument("--compacta", action="store_true",
                    help="VACUUM si sobra espai, només de matinada (el timer)")
    args = ap.parse_args()
    if args.compacta:
        compacta(DB)
        return
    t = time.time()
    con = sqlite3.connect(DB, timeout=120)
    con.execute("PRAGMA secure_delete=OFF")
    print(purgar(con))
    con.close()
    if args.vacuum:
        vacuum(DB)
    print(f"hecho en {time.time() - t:.0f} s; tamaño {DB.stat().st_size / 2**30:.2f} GB")


if __name__ == "__main__":
    main()

"""
Poller: sondea el endpoint info-estacion de Metrovalencia para todas las
estaciones, parsea las próximas llegadas y las persiste en SQLite.

Uso:
    python -m backend.poller --once      # una pasada
    python -m backend.poller             # bucle continuo (INTERVAL s)

Tablas SQLite (data/metro.db):
    snapshots(id, ts)                                  -- una fila por pasada
    arrivals(snapshot_id, ts, estacion_id, linea, destino, eta_seg, eta_txt, ocupacion)
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import requests

# El certificado de metrovalencia.es llega con la cadena incompleta (falta el
# intermedio). truststore usa el almacén del SO, que la completa vía AIA.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - en Linux suele no hacer falta
    pass

from .network import load_network
from .parser import parse_arrivals

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"

AJAX_URL = "https://www.metrovalencia.es/wp-admin/admin-ajax.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.metrovalencia.es/ca/consulta-estaciones/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
INTERVAL = 30          # segundos entre pasadas completas
REQ_PAUSE = 0.15       # pausa entre estaciones (cortesía con el servidor)
TIMEOUT = 15


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS arrivals (
    snapshot_id INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    estacion_id INTEGER NOT NULL,
    linea       INTEGER NOT NULL,
    destino     TEXT    NOT NULL,
    eta_seg     INTEGER NOT NULL,
    eta_txt     TEXT,
    ocupacion   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_arr_snap ON arrivals(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_arr_est  ON arrivals(estacion_id, ts);
"""


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.commit()
    return con


def fetch_station(session: requests.Session, sid: int) -> str | None:
    body = f"action=formularios_ajax&data=action%3Dinfo-estacion%26id%3D{sid}"
    try:
        r = session.post(AJAX_URL, data=body, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and len(r.text) > 5:
            return r.json().get("html")
    except (requests.RequestException, ValueError):
        return None
    return None


def poll_once(con: sqlite3.Connection, session: requests.Session) -> int:
    net = load_network()
    ts = int(time.time())
    cur = con.execute("INSERT INTO snapshots(ts) VALUES (?)", (ts,))
    snap_id = cur.lastrowid
    rows = []
    ok = 0
    for sid in net.estaciones:
        html = fetch_station(session, sid)
        if html is None:
            continue
        ok += 1
        for a in parse_arrivals(html):
            rows.append(
                (snap_id, ts, sid, a.linea, a.destino, a.eta_seg, a.eta_txt, a.ocupacion)
            )
        time.sleep(REQ_PAUSE)
    con.executemany(
        "INSERT INTO arrivals(snapshot_id,ts,estacion_id,linea,destino,eta_seg,eta_txt,ocupacion)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    print(f"[{time.strftime('%H:%M:%S')}] snapshot {snap_id}: "
          f"{ok}/{len(net.estaciones)} estaciones, {len(rows)} llegadas")
    return snap_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="una sola pasada y salir")
    ap.add_argument("--interval", type=int, default=INTERVAL)
    args = ap.parse_args()

    con = init_db()
    session = requests.Session()
    if args.once:
        poll_once(con, session)
        return
    while True:
        t0 = time.time()
        try:
            poll_once(con, session)
        except Exception as e:  # noqa: BLE001 - el bucle no debe morir
            print("error en pasada:", e)
        dt = time.time() - t0
        time.sleep(max(1.0, args.interval - dt))


if __name__ == "__main__":
    main()

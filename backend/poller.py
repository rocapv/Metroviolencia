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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .network import load_network
from .parser import parse_arrivals

# El certificado de metrovalencia.es llega con la cadena incompleta (falta el
# intermedio de Sectigo). Estrategia de verificación TLS, por orden:
#   1) data/ca-bundle.pem si existe (certifi + intermedio; ver build_ca_bundle.py)
#   2) truststore (almacén del SO; resuelve AIA en Windows)
#   3) verificación por defecto de requests
_CA_BUNDLE = Path(__file__).resolve().parent.parent / "data" / "ca-bundle.pem"
VERIFY: object = True
if _CA_BUNDLE.exists():
    VERIFY = str(_CA_BUNDLE)
else:
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
CLIENT_FILE = ROOT / "data" / "last_client.txt"

AJAX_URL = "https://www.metrovalencia.es/wp-admin/admin-ajax.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.metrovalencia.es/ca/consulta-estaciones/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
INTERVAL = 300          # cadencia SIN clientes (5 min)
INTERVAL_ACTIVO = 60    # cadencia CON cliente activo (1 min)
CLIENTE_VENTANA = 130   # s: cliente "activo" si pidió datos hace menos de esto
WORKERS = 12            # estaciones en paralelo. Barrido: ~62s en serie -> ~22s.
                        # El techo lo pone Metrovalencia (~7 req/s por IP): subir
                        # workers da rendimientos decrecientes (24->20s, 32->17s)
                        # a costa de castigar su servidor. 12 = buen equilibrio.
TIMEOUT = 15


def cliente_activo() -> bool:
    """True si algún cliente pidió datos hace poco (lo marca la API)."""
    try:
        ts = float(CLIENT_FILE.read_text(encoding="utf-8").strip())
        return (time.time() - ts) < CLIENTE_VENTANA
    except Exception:  # noqa: BLE001
        return False


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

-- Registro NoInfo: por pasada, las líneas esperadas que NO dieron ninguna
-- llegada (la fuente no informó). Sirve para saber "cuándo faltó dato".
CREATE TABLE IF NOT EXISTS noinfo (
    snapshot_id INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    linea       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_noinfo_ts ON noinfo(linea, ts);
"""

# Líneas de METRO con tiempo real en la fuente. Si alguna no aparece en una
# pasada -> se registra NoInfo. Los tranvías (4,6,8) nunca dan datos y no cuentan.
LINEAS_ESPERADAS = [1, 2, 3, 5, 7, 9, 10]


def make_session() -> requests.Session:
    """Session con el pool de conexiones dimensionado a WORKERS: por defecto
    requests limita a 10 conexiones concurrentes por host y estrangula el
    barrido paralelo. Igualarlo a WORKERS deja correr los hilos de verdad."""
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=WORKERS, pool_maxsize=WORKERS
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.commit()
    return con


def fetch_station(session: requests.Session, sid: int) -> str | None:
    body = f"action=formularios_ajax&data=action%3Dinfo-estacion%26id%3D{sid}"
    try:
        r = session.post(AJAX_URL, data=body, headers=HEADERS, timeout=TIMEOUT,
                         verify=VERIFY)
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
    # Barrido en paralelo (WORKERS hilos): las 144 estaciones son peticiones
    # independientes; el suelo de latencia deja de ser ~62s (serie) y baja a ~6s,
    # así la cadencia de 60s con cliente activo entrega datos realmente frescos.
    # La concurrencia acotada hace de cortesía con el servidor (antes: REQ_PAUSE).
    def _one(sid: int):
        return sid, fetch_station(session, sid)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        resultados = ex.map(_one, list(net.estaciones))
    for sid, html in resultados:
        if html is None:
            continue
        ok += 1
        for a in parse_arrivals(html):
            rows.append(
                (snap_id, ts, sid, a.linea, a.destino, a.eta_seg, a.eta_txt, a.ocupacion)
            )
    con.executemany(
        "INSERT INTO arrivals(snapshot_id,ts,estacion_id,linea,destino,eta_seg,eta_txt,ocupacion)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    # NoInfo: líneas esperadas sin ninguna llegada en esta pasada
    lineas_con_datos = {r[3] for r in rows}
    ni = [(snap_id, ts, l) for l in LINEAS_ESPERADAS if l not in lineas_con_datos]
    if ni:
        con.executemany("INSERT INTO noinfo(snapshot_id,ts,linea) VALUES (?,?,?)", ni)
    con.commit()
    faltan = ", ".join(f"L{l}" for _s, _t, l in ni) or "-"
    print(f"[{time.strftime('%H:%M:%S')}] snapshot {snap_id}: "
          f"{ok}/{len(net.estaciones)} estaciones, {len(rows)} llegadas, NoInfo: {faltan}")
    return snap_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="una sola pasada y salir")
    ap.add_argument("--interval", type=int, default=INTERVAL)
    args = ap.parse_args()

    con = init_db()
    session = make_session()
    if args.once:
        poll_once(con, session)
        return

    idle = args.interval            # cadencia sin clientes (por defecto 300)
    last_pull = 0.0
    while True:
        objetivo = INTERVAL_ACTIVO if cliente_activo() else idle
        transcurrido = time.time() - last_pull
        if transcurrido >= objetivo:
            # (A) el intervalo se cuenta desde el INICIO de la pasada, no del
            # final: así el ciclo es max(objetivo, duración) y no objetivo+duración.
            last_pull = time.time()
            try:
                poll_once(con, session)
            except Exception as e:  # noqa: BLE001 - el bucle no debe morir
                print("error en pasada:", e)
        else:
            # re-evalúa a menudo para reaccionar rápido cuando entra un cliente
            time.sleep(min(15.0, objetivo - transcurrido + 0.1))


if __name__ == "__main__":
    main()

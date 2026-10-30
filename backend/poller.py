"""
Poller: sondea el endpoint info-estacion de Metrovalencia para todas las
estaciones, parsea las próximas llegadas y las persiste en SQLite.

Uso:
    python -m backend.poller --once      # una pasada
    python -m backend.poller             # bucle continuo (INTERVAL s)

Tablas SQLite (data/metro.db):
    snapshots(id, ts)                                  -- una fila por pasada
    arrivals(snapshot_id, ts, estacion_id, linea, destino, eta_seg, eta_txt, ocupacion)
    horario_prog / horario_descargas   -- ver horarios.py (salidas programadas)
    salidas_obs                        -- ver salidas.py (salidas reales)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import horarios, pasos, purga, salidas, xat
from .cabeceras import CABECERAS
from .network import load_network
from .parser import parse_arrivals
from .positions import estimar
from .tracking import track

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
PURGA_FILE = ROOT / "data" / "ultima_purga.txt"   # marca de la última purga diaria

AJAX_URL = "https://www.metrovalencia.es/wp-admin/admin-ajax.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.metrovalencia.es/ca/consulta-estaciones/",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}
INTERVAL = 120          # cadencia SIN clientes: 2 min (30/10/2026; antes 1 min)
INTERVAL_ACTIVO = 120   # cadencia CON cliente activo (2 min; antes 1 min)
INTERVAL_NOCHE = 600    # cadencia FUERA del horario de servicio: una pasada
                        # centinela cada 10 min (de madrugada el 74 % de las
                        # pasadas venían vacías: 144 peticiones para nada)
MARGEN_SERVICIO = 1200  # s: "hay servicio" si alguna hora de paso programada
                        # cae a menos de 20 min de ahora (antes/después)
FORZAR_SERVICIO = 1800  # s: si una pasada centinela ve trenes, cadencia normal
                        # durante este tiempo (servicio especial o horario mal)
CLIENTE_VENTANA = 250   # s: cliente "activo" si pidió datos hace menos de esto
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


_serv_cache: dict[str, list[int]] = {}   # dia -> horas de paso programadas (ordenadas)
ULTIMAS_LLEGADAS = 0                      # llegadas de la última pasada (lo lee el bucle)


def en_servicio(con: sqlite3.Connection, ts: int) -> bool:
    """¿Hay servicio programado alrededor de `ts`? Mira el horario de paso de
    los días que toca la ventana [ts-MARGEN, ts+MARGEN] (las horas de después
    de medianoche van con el día natural en que caen). Sin horario descargado
    para esos días -> True: sin información no se deja de sondear."""
    import bisect
    dias = {dt.date.fromtimestamp(t).isoformat()
            for t in (ts - MARGEN_SERVICIO, ts, ts + MARGEN_SERVICIO)}
    alguno = False
    for dia in sorted(dias):
        if dia not in _serv_cache:
            try:
                horas = sorted(t for _s, _v, t in pasos.horas_dia(con, dia))
            except sqlite3.OperationalError:
                horas = []
            if not horas:
                return True                       # ese día aún sin horario
            for k in [k for k in _serv_cache if k < min(dias)]:
                del _serv_cache[k]                # días pasados fuera de la caché
            _serv_cache[dia] = horas
        alguno = True
        horas = _serv_cache[dia]
        i = bisect.bisect_left(horas, ts - MARGEN_SERVICIO)
        if i < len(horas) and horas[i] <= ts + MARGEN_SERVICIO:
            return True
    return not alguno


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,          -- inicio de la pasada (referencia de las ETAs)
    ts_fin INTEGER                    -- fin de la pasada: los datos ya están servidos
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

-- Trenes físicos estimados y con IDENTIDAD persistente (track_id) entre pulls.
-- Se rellena una vez por pasada (tracking.track). track_id sigue al mismo tren
-- de un snapshot al siguiente aunque la fuente no numere los trenes; con el
-- histórico (track_id, ts) se podrá calcular luego el delay vs horario.
CREATE TABLE IF NOT EXISTS trenes (
    snapshot_id   INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    track_id      INTEGER NOT NULL,
    linea         INTEGER NOT NULL,
    destino       TEXT    NOT NULL,
    color         TEXT,
    lat           REAL,
    lng           REAL,
    prox_estacion TEXT,
    eta_seg       INTEGER,
    origen        INTEGER,
    d             REAL,
    waypoints     TEXT,
    via           INTEGER         -- línea comercial que da la geometría (linea o variante de ramal)
);
CREATE INDEX IF NOT EXISTS idx_trenes_snap  ON trenes(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_trenes_track ON trenes(track_id, ts);

-- Lecturas SIN información: estación que no se pudo leer (motivo 'error') o
-- cabecera leída con el panel VACÍO (motivo 'vacia', p. ej. de madrugada). Con
-- esto el registro distingue "cancelado" de "sin datos" sin depender de
-- `arrivals`, que se purga a los pocos días (ver purga.py).
CREATE TABLE IF NOT EXISTS lecturas_fallidas (
    snapshot_id INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    estacion_id INTEGER NOT NULL,
    motivo      TEXT NOT NULL DEFAULT 'error'
);
CREATE INDEX IF NOT EXISTS idx_fallidas ON lecturas_fallidas(estacion_id, ts);
"""

# Líneas con tiempo real en la fuente: metro (1,2,3,5,7,9) y tranvía (4,6,8,10;
# L10 también es tranvía). Desde septiembre los tranvías también dan datos. Si
# alguna no aparece en una pasada -> se registra NoInfo.
LINEAS_ESPERADAS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


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
    con = sqlite3.connect(DB, timeout=60)
    try:   # bases anteriores a la columna motivo
        con.execute("ALTER TABLE lecturas_fallidas ADD COLUMN motivo TEXT NOT NULL DEFAULT 'error'")
    except sqlite3.OperationalError:
        pass
    try:   # bases anteriores a ts_fin (fin de pasada; el cliente lo usa para sincronizarse)
        con.execute("ALTER TABLE snapshots ADD COLUMN ts_fin INTEGER")
    except sqlite3.OperationalError:
        pass
    try:   # bases anteriores a `via` (variante de ramal que da la geometría del tren)
        con.execute("ALTER TABLE trenes ADD COLUMN via INTEGER")
    except sqlite3.OperationalError:
        pass
    con.executescript(SCHEMA)
    con.commit()
    horarios.init_db(con)
    salidas.init_db(con)
    pasos.init_db(con)
    xat.init_db(con)
    return con


def fetch_station(session: requests.Session, sid: int) -> tuple[str | None, str]:
    """(html, motivo): html None si la lectura falla; motivo dice por qué
    ('error:timeout', 'error:conexion', 'error:http503', 'error:cuerpo'...)."""
    body = f"action=formularios_ajax&data=action%3Dinfo-estacion%26id%3D{sid}"
    try:
        r = session.post(AJAX_URL, data=body, headers=HEADERS, timeout=TIMEOUT,
                         verify=VERIFY)
    except requests.Timeout:
        return None, "error:timeout"
    except requests.exceptions.SSLError:
        return None, "error:tls"
    except requests.ConnectionError:
        return None, "error:conexion"
    except requests.RequestException:
        return None, "error:peticion"
    if r.status_code != 200:
        return None, f"error:http{r.status_code}"
    if len(r.text) <= 5:
        return None, "error:cuerpo"           # FGV contesta "0" o vacío
    try:
        return r.json().get("html"), ""
    except ValueError:
        return None, "error:json"


def poll_once(con: sqlite3.Connection, session: requests.Session) -> int:
    net = load_network()
    ts = int(time.time())
    cur = con.execute("INSERT INTO snapshots(ts) VALUES (?)", (ts,))
    snap_id = cur.lastrowid
    rows = []
    ok = 0
    por_estacion: dict[int, list] = {}   # estaciones leídas bien -> llegadas
    # Barrido en paralelo (WORKERS hilos): las 144 estaciones son peticiones
    # independientes; el suelo de latencia deja de ser ~62s (serie) y baja a ~6s,
    # así la cadencia de 60s con cliente activo entrega datos realmente frescos.
    # La concurrencia acotada hace de cortesía con el servidor (antes: REQ_PAUSE).
    def _one(sid: int):
        return sid, *fetch_station(session, sid)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        resultados = ex.map(_one, list(net.estaciones))
    fallidas = []
    for sid, html, motivo in resultados:
        if html is None:
            fallidas.append((snap_id, ts, sid, motivo))
            continue
        ok += 1
        lleg = por_estacion.setdefault(sid, [])
        for a in parse_arrivals(html):
            lleg.append((a.linea, a.destino, a.eta_seg))
            rows.append(
                (snap_id, ts, sid, a.linea, a.destino, a.eta_seg, a.eta_txt, a.ocupacion)
            )
    # cabeceras leídas pero con el panel vacío: tampoco hay información
    fallidas = fallidas + [
        (snap_id, ts, c["id"], "vacia") for c in CABECERAS
        if c["id"] in por_estacion and not por_estacion[c["id"]]]
    if fallidas:
        con.executemany("INSERT INTO lecturas_fallidas(snapshot_id,ts,estacion_id,motivo)"
                        " VALUES (?,?,?,?)", fallidas)
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

    # SALIDAS reales desde cabecera (registro programado vs real)
    sal = salidas.actualizar(con, ts, por_estacion)
    # PASOS reales por TODAS las estaciones (retrasos en ruta)
    pas = pasos.actualizar(con, ts, por_estacion, net)

    # TRACKING: estima los trenes físicos de esta pasada y les da identidad
    # persistente (track_id), siguiéndolos desde el snapshot anterior.
    n_tr = track_snapshot(con, net, snap_id, ts, rows)

    global ULTIMAS_LLEGADAS
    ULTIMAS_LLEGADAS = len(rows)
    # fin de la pasada: desde aquí los trenes de este snapshot están servidos
    ts_fin = int(time.time())
    con.execute("UPDATE snapshots SET ts_fin=? WHERE id=?", (ts_fin, snap_id))
    con.commit()
    faltan = ", ".join(f"L{l}" for _s, _t, l in ni) or "-"
    print(f"[{time.strftime('%H:%M:%S')}] snapshot {snap_id} ({ts_fin - ts} s): "
          f"{ok}/{len(net.estaciones)} estaciones, {len(rows)} llegadas, "
          f"{n_tr} trenes, NoInfo: {faltan}, salidas: {sal['salen']} "
          f"(+{sal['nuevas']} previstas, {sal['desaparecen']} caídas), "
          f"pasos: {pas['salen']}")
    return snap_id


def track_snapshot(con: sqlite3.Connection, net, snap_id: int, ts: int, rows: list) -> int:
    """Estima los trenes de esta pasada y les asigna track_id persistente,
    emparejándolos con los del snapshot anterior. Persiste en la tabla `trenes`.
    Devuelve el nº de trenes emitidos."""
    # rows = (snap_id, ts, sid, linea, destino, eta_seg, eta_txt, ocupacion)
    llegadas = [(r[2], r[3], r[4], r[5]) for r in rows]
    curr = estimar(net, llegadas)

    # snapshot anterior CON trenes (el estado de seguimiento vive en la propia tabla)
    prev_row = con.execute(
        "SELECT snapshot_id, ts FROM trenes ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    prev, dt = [], 0.0
    if prev_row:
        psnap, pts = prev_row[0], prev_row[1]
        dt = float(ts - pts)
        for r in con.execute(
            "SELECT track_id,linea,destino,waypoints,via FROM trenes WHERE snapshot_id=?",
            (psnap,),
        ):
            prev.append({"track_id": r[0], "linea": r[1], "destino": r[2],
                         "waypoints": json.loads(r[3]), "via": r[4] or r[1]})
    next_id = con.execute(
        "SELECT COALESCE(MAX(track_id),0)+1 FROM trenes"
    ).fetchone()[0]

    vias = {k: (lc.variante_de, lc.d_union) for k, lc in net.comerciales.items() if lc.variante_de}
    ids, _ = track(curr, prev, dt, next_id, vias=vias)
    filas = []
    for i, t in enumerate(curr):
        d0 = t.waypoints[0]["d"] if t.waypoints else None
        filas.append((snap_id, ts, ids[i], t.linea, t.destino, t.color, t.lat, t.lng,
                      t.prox_estacion, t.eta_seg, 1 if t.origen else 0, d0,
                      json.dumps(t.waypoints), t.via or t.linea))
    con.executemany(
        "INSERT INTO trenes(snapshot_id,ts,track_id,linea,destino,color,lat,lng,"
        "prox_estacion,eta_seg,origen,d,waypoints,via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        filas,
    )
    con.commit()
    return len(filas)


_hilo_horario: threading.Thread | None = None


def _descargar_horarios() -> None:
    """Horario programado de hoy y mañana (idempotente: solo lo que falte).
    En hilo aparte con su propia conexión: son ~40 consultas lentas y no deben
    retrasar el pull de 1 min."""
    try:
        con = sqlite3.connect(DB, timeout=60)
        net = load_network()
        s = make_session()
        hoy = dt.date.today()
        for d in (hoy, hoy + dt.timedelta(days=1)):
            n = horarios.descargar_dia(con, s, HEADERS, VERIFY, d.isoformat(), net)
            if n:
                print(f"horario {d}: {n} salidas programadas descargadas")
        for d in (hoy, hoy + dt.timedelta(days=1)):   # horario de paso (~7 min/día)
            n = pasos.descargar_dia(con, s, HEADERS, VERIFY, d.isoformat(), net)
            if n:
                print(f"horario de paso {d}: {n} horas descargadas")
        # purga diaria de datos brutos (arrivals/trenes antiguos), ver purga.py
        try:
            ultima = float(PURGA_FILE.read_text().strip())
        except Exception:  # noqa: BLE001
            ultima = 0.0
        if time.time() - ultima > 20 * 3600:
            print("purga:", purga.purgar(con))
            PURGA_FILE.write_text(str(int(time.time())))
        con.close()
    except Exception as e:  # noqa: BLE001
        print("error descargando horario:", e)


def lanzar_horarios() -> None:
    global _hilo_horario
    if _hilo_horario and _hilo_horario.is_alive():
        return
    _hilo_horario = threading.Thread(target=_descargar_horarios, daemon=True)
    _hilo_horario.start()


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

    idle = args.interval            # cadencia sin clientes (por defecto 60)
    last_pull = 0.0
    last_horario = 0.0
    forzado_hasta = 0.0             # una centinela vio trenes: servicio hasta aquí
    modo = "servicio"
    while True:
        if time.time() - last_horario > 3600:   # cada hora: ¿falta horario?
            last_horario = time.time()
            lanzar_horarios()
        ahora = time.time()
        if cliente_activo():
            objetivo = INTERVAL_ACTIVO
        elif ahora < forzado_hasta or en_servicio(con, int(ahora)):
            objetivo = idle
        else:
            objetivo = INTERVAL_NOCHE            # fuera de horario: centinela
        nuevo = "noche" if objetivo == INTERVAL_NOCHE else "servicio"
        if nuevo != modo:
            modo = nuevo
            print(f"[{time.strftime('%H:%M:%S')}] modo {modo}: pasada cada {objetivo} s")
        transcurrido = ahora - last_pull
        if transcurrido >= objetivo:
            # (A) el intervalo se cuenta desde el INICIO de la pasada, no del
            # final: así el ciclo es max(objetivo, duración) y no objetivo+duración.
            last_pull = ahora
            try:
                poll_once(con, session)
                if modo == "noche" and ULTIMAS_LLEGADAS:
                    # hay trenes fuera del horario (servicio especial, horario
                    # incompleto...): vuelve a la cadencia normal un rato
                    forzado_hasta = time.time() + FORZAR_SERVICIO
                    print(f"  centinela: {ULTIMAS_LLEGADAS} llegadas fuera de horario, "
                          f"cadencia normal {FORZAR_SERVICIO // 60} min")
            except Exception as e:  # noqa: BLE001 - el bucle no debe morir
                print("error en pasada:", e)
        else:
            # re-evalúa a menudo para reaccionar rápido cuando entra un cliente
            time.sleep(min(15.0, objetivo - transcurrido + 0.1))


if __name__ == "__main__":
    main()

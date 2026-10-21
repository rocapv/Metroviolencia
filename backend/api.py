"""
API de Metroviolencia (FastAPI).

Endpoints:
    GET /api/network   -> geometría estática (líneas, estaciones, colores)
    GET /api/trains    -> trenes estimados del último snapshot
    GET /api/arrivals  -> llegadas crudas del último snapshot (por estación)
    GET /api/ruta      -> ruta origen->destino y trenes en vivo a coger (?origen=&destino=)
    GET /api/retards_ruta -> retrasos al pasar por cada estación (?dia=YYYY-MM-DD)
    GET /api/registro  -> salidas programadas vs reales de un día (?dia=YYYY-MM-DD)
    GET /api/ultimo    -> marca de la última pasada servida (+ último id del xat)
    GET /api/xat       -> mensajes: ?tipus=&lloc= (los de un lugar) o ?des_de=ID (feed)
    POST /api/xat      -> publica un mensaje {tipus, lloc, alias, text}
    GET /api/health    -> estado

Sirve además el frontend estático de ../web en la raíz.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pasos, rutas, salidas, xat
from .calendario import tipo as tipo_dia
from .network import load_network
from .positions import estimar

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
WEB = ROOT / "web"
TOKEN_FILE = ROOT / "data" / "token.txt"
CLIENT_FILE = ROOT / "data" / "last_client.txt"  # señal de "hay cliente activo"
OPEN_FILE = ROOT / "data" / "open_until.txt"      # ventana temporal SIN token

app = FastAPI(title="Metroviolencia", version="0.1")


def _touch_client() -> None:
    """Marca que un cliente acaba de pedir datos (el poller usa esto para pasar
    a cadencia rápida de 1 min; sin clientes vuelve a 5 min)."""
    try:
        CLIENT_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _token() -> str:
    """Token de acceso (hardcodeado en la APK). Si no hay, acceso abierto (dev)."""
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("METRO_TOKEN", "").strip()


def _is_lan(ip: str) -> bool:
    """Rangos de red local (RFC1918) + loopback -> sin token."""
    return (
        ip in ("127.0.0.1", "::1", "localhost")
        or ip.startswith("192.168.")
        or ip.startswith("10.")
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )


def _open_now() -> bool:
    """Ventana en la que NO se exige token (ni siquiera desde fuera).
    Caduca sola cuando pasa el instante guardado en data/open_until.txt; si el
    fichero dice `siempre`, no caduca (se levanta borrando el fichero)."""
    try:
        valor = OPEN_FILE.read_text(encoding="utf-8").strip()
        return valor.lower() == "siempre" or time.time() < float(valor)
    except Exception:  # noqa: BLE001
        return False


def _client_ip(request: Request) -> str:
    # Apache (mod_proxy) AÑADE la IP real al final de X-Forwarded-For; el último
    # valor es el que vio Apache = cliente real (no falsificable por el cliente).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


@app.middleware("http")
async def auth_token(request: Request, call_next):
    """En RED LOCAL no hace falta token. Desde FUERA de la LAN el token es el
    secreto obligatorio: sin él, aun con IP/puerto, la web no responde (403).
    La APK abre .../?token=XXX una vez; se fija la cookie y el resto de
    peticiones (app.js, /api/...) van autenticadas."""
    q = request.query_params.get("token")
    if _is_lan(_client_ip(request)) or _open_now():
        resp = await call_next(request)   # LAN o ventana temporal abierta: sin token
    else:
        token = _token()
        ok = bool(token) and (q == token or request.cookies.get("mvtoken") == token)
        if not ok:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        resp = await call_next(request)
        if q == token:   # primer acceso externo con token en la URL: fija cookie
            # En la ruta heredada (home.pr1.es:5580/metroviolencia/) la app cuelga de
            # /metroviolencia; en metroviolencia.pr1.es cuelga de la raíz y su vhost de
            # Apache manda X-Forwarded-Prefix: "/" (y X-Forwarded-Proto: https -> Secure).
            raiz = request.headers.get("x-forwarded-prefix") == "/"
            resp.set_cookie("mvtoken", token, max_age=31536000,
                            path="/" if raiz else "/metroviolencia", httponly=True, samesite="lax",
                            secure=request.headers.get("x-forwarded-proto") == "https")
    # nunca cachear: ni los datos en vivo ni el frontend (evita snapshots viejos
    # y que el WebView sirva un app.js antiguo). Robusto ante root_path del proxy.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _last_snapshot(con) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()


@app.get("/api/network")
def network():
    return load_network().to_geojson_meta()


@app.get("/api/arrivals")
def arrivals():
    con = _con()
    snap = _last_snapshot(con)
    if not snap:
        return {"ts": None, "estaciones": {}}
    rows = con.execute(
        "SELECT estacion_id,linea,destino,eta_seg,eta_txt FROM arrivals "
        "WHERE snapshot_id=? ORDER BY estacion_id,eta_seg",
        (snap["id"],),
    ).fetchall()
    por_est: dict[int, list] = {}
    for r in rows:
        por_est.setdefault(r["estacion_id"], []).append(
            {"linea": r["linea"], "destino": r["destino"],
             "eta_seg": r["eta_seg"], "eta_txt": r["eta_txt"]}
        )
    return {"ts": snap["ts"], "edad_seg": int(time.time()) - snap["ts"],
            "estaciones": por_est}


def _noinfo(con, snap_id: int) -> list:
    try:
        return [r["linea"] for r in con.execute(
            "SELECT linea FROM noinfo WHERE snapshot_id=? ORDER BY linea", (snap_id,)
        ).fetchall()]
    except sqlite3.OperationalError:
        return []  # tabla aún no creada


def _ts_fin(con, snap_id: int) -> int | None:
    """Fin de la pasada (None en bases sin la columna o pasadas antiguas)."""
    try:
        r = con.execute("SELECT ts_fin FROM snapshots WHERE id=?", (snap_id,)).fetchone()
        return r["ts_fin"] if r else None
    except sqlite3.OperationalError:
        return None


@app.get("/api/ultimo")
def ultimo():
    """Marca de la última pasada servida (ts de inicio y ts_fin), para que el
    cliente se sincronice con el poller y pida /api/trains justo cuando hay
    datos nuevos, en vez de cada minuto a ciegas. Barato: se consulta cada 2 s."""
    _touch_client()
    con = _con()
    try:
        row = con.execute(
            "SELECT snapshot_id, ts FROM trenes ORDER BY snapshot_id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        row = None
    xid = xat.ultim_id(con)
    if row is None:
        return {"ts": None, "ts_fin": None, "srv_ms": int(time.time() * 1000), "xat_id": xid}
    return {"ts": row["ts"], "ts_fin": _ts_fin(con, row["snapshot_id"]),
            "srv_ms": int(time.time() * 1000), "xat_id": xid}


@app.get("/api/xat")
def xat_get(tipus: str | None = None, lloc: int | None = None, des_de: int | None = None,
            n: int = 20):
    """Mensajes del xat: de un lugar (?tipus=tren|estacio&lloc=ID) o el feed
    general (?des_de=ID -> los posteriores a ese id; des_de=0 -> los últimos)."""
    con = _con()
    n = max(1, min(int(n), xat.MAX_FEED))
    if tipus is not None and lloc is not None:
        if tipus not in ("tren", "estacio"):
            return JSONResponse({"error": "tipus invàlid"}, status_code=400)
        return {"tipus": tipus, "lloc": lloc, "missatges": xat.del_lloc(con, tipus, lloc, n)}
    return {"des_de": des_de or 0, "ultim_id": xat.ultim_id(con),
            "missatges": xat.des_de(con, des_de or 0, n)}


@app.post("/api/xat")
async def xat_post(request: Request):
    """Publica un mensaje desde un tren (track_id vivo) o una estación."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "JSON invàlid"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON invàlid"}, status_code=400)
    con = _con()
    msg, err = xat.publicar(con, _net(), _client_ip(request), str(body.get("tipus", "")),
                            body.get("lloc"), str(body.get("alias", "")), str(body.get("text", "")))
    if msg is None:
        return JSONResponse({"error": err}, status_code=429 if "espera" in err or "massa" in err else 400)
    return msg


@app.get("/api/trains")
def trains():
    _touch_client()   # hay cliente activo -> el poller irá a 1 min
    con = _con()
    # camino normal: el poller ya estimó y trackeó los trenes de la última pasada;
    # devolvemos esos, con su `id` persistente (mismo tren entre pulls).
    try:
        row = con.execute(
            "SELECT snapshot_id, ts FROM trenes ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None  # tabla aún no creada (poller viejo)
    if row is not None:
        snap_id, ts = row["snapshot_id"], row["ts"]
        trs = con.execute(
            "SELECT track_id,linea,destino,color,lat,lng,prox_estacion,eta_seg,"
            "origen,waypoints FROM trenes WHERE snapshot_id=?", (snap_id,)
        ).fetchall()
        trenes = [{
            "id": r["track_id"], "linea": r["linea"], "destino": r["destino"],
            "color": r["color"], "lat": r["lat"], "lng": r["lng"],
            "prox_estacion": r["prox_estacion"], "eta_seg": r["eta_seg"],
            "origen": bool(r["origen"]), "waypoints": json.loads(r["waypoints"]),
        } for r in trs]
        # retraso en vivo frente al horario de paso (se ve al tocar el tren)
        for t in trenes:
            try:
                t["retraso"] = None if t["origen"] else pasos.retraso_vivo(con, _net(), t, ts)
            except sqlite3.OperationalError:
                t["retraso"] = None
        return {"ts": ts, "ts_fin": _ts_fin(con, snap_id), "edad_seg": int(time.time()) - ts,
                "srv_ms": int(time.time() * 1000),
                "n": len(trenes), "trenes": trenes, "noinfo": _noinfo(con, snap_id)}

    # fallback (DB sin tracking aún): estima al vuelo, sin identidad persistente
    snap = _last_snapshot(con)
    if not snap:
        return {"ts": None, "trenes": []}
    rows = con.execute(
        "SELECT estacion_id,linea,destino,eta_seg FROM arrivals WHERE snapshot_id=?",
        (snap["id"],),
    ).fetchall()
    llegadas = [(r["estacion_id"], r["linea"], r["destino"], r["eta_seg"]) for r in rows]
    net = load_network()
    trenes = [t.to_dict() for t in estimar(net, llegadas)]
    return {"ts": snap["ts"], "edad_seg": int(time.time()) - snap["ts"],
            "srv_ms": int(time.time() * 1000),
            "n": len(trenes), "trenes": trenes, "noinfo": _noinfo(con, snap["id"])}


@app.on_event("startup")
def _precalienta():
    """Arranca en segundo plano el cálculo diario de paradas incoherentes."""
    import datetime as dt
    try:
        pasos.incoherentes(None, _net(), dt.date.today().isoformat())
    except Exception:  # noqa: BLE001
        pass


_net_cache = None
_sesion = None


def _net():
    global _net_cache
    if _net_cache is None:
        _net_cache = load_network()
    return _net_cache


@app.get("/api/ruta")
def ruta(origen: int, destino: int):
    """Alternativas del planificador oficial + trenes en vivo de cada paso."""
    global _sesion
    from .poller import HEADERS, VERIFY, make_session
    if origen == destino:
        return JSONResponse({"error": "origen y destino iguales"}, status_code=400)
    net = _net()
    if origen not in net.estaciones or destino not in net.estaciones:
        return JSONResponse({"error": "estación desconocida"}, status_code=400)
    _sesion = _sesion or make_session()
    try:
        res = rutas.consultar_ruta(_sesion, HEADERS, VERIFY, origen, destino)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"planificador no disponible: {e}"}, status_code=502)
    vivo = trains()
    con = _con()
    snap = _last_snapshot(con)
    llegadas: dict[int, list] = {}
    if snap:
        for r in con.execute("SELECT estacion_id,linea,destino,eta_seg FROM arrivals "
                             "WHERE snapshot_id=?", (snap["id"],)):
            llegadas.setdefault(r[0], []).append((r[1], r[2], r[3]))
    alts = rutas.enriquecer(
        net, [dict(a, pasos=[dict(p) for p in a["pasos"]]) for a in res["alternativas"]],
        vivo.get("trenes", []), llegadas, res["dia"], vivo.get("ts"))
    return {"origen": origen, "destino": destino, "ts": vivo.get("ts"),
            "dia": res["dia"], "ventana": res["ventana"], "alternativas": alts}


_ret_cache: dict[str, tuple[float, dict]] = {}


@app.get("/api/retards_ruta")
def retards_ruta(dia: str | None = None):
    """Retrasos EN RUTA: pasos reales por cada estación frente a su horario."""
    import datetime as dt
    dia = dia or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(dia)
    except ValueError:
        return JSONResponse({"error": "dia inválido"}, status_code=400)
    c = _ret_cache.get(dia)
    # un día ya cerrado no cambia: su cálculo (~6 s en la Raspa) vale para siempre
    if c and (dia < dt.date.today().isoformat() or time.time() - c[0] < 55):
        return c[1]
    con = _con()
    ahora = int(time.time())
    # día antiguo ya resumido por la purga: sus pasos crudos ya no están
    guardado = pasos.resumen_guardado(con, dia) if dia < dt.date.today().isoformat() else None
    if guardado is not None:
        res = {"dia": dia, "ahora": ahora, "n_pasos": sum(v["n"] for v in guardado.values()),
               "ultima_hora": {}, "dia_completo": guardado, "tarde_recientes": [],
               "resumido": True}
        _ret_cache[dia] = (time.time(), res)
        return res
    try:
        filas = pasos.retrasos(con, _net(), dia)
    except sqlite3.OperationalError:
        filas = []
    tarde = [f for f in filas if f["retraso_s"] >= pasos.TARDE and f["ts_paso"] >= ahora - 2700]
    tarde.sort(key=lambda f: -f["ts_paso"])
    res = {"dia": dia, "ahora": ahora, "n_pasos": len(filas),
           "ultima_hora": pasos.resumen_lineas(filas, ahora - 3600),
           "dia_completo": pasos.resumen_lineas(filas),
           "tarde_recientes": tarde[:120]}
    _ret_cache[dia] = (time.time(), res)
    return res


@app.get("/api/registro")
def registro(dia: str | None = None):
    """Registro programado vs real de las salidas desde cabecera de `dia`."""
    import datetime as dt
    dia = dia or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(dia)
    except ValueError:
        return JSONResponse({"error": "dia inválido"}, status_code=400)
    con = _con()
    try:
        filas = salidas.registro(con, dia)
    except sqlite3.OperationalError:
        filas = []   # tablas aún no creadas
    return {"dia": dia, "ahora": int(time.time()),
            "tipo_dia": {"metro": tipo_dia(dia, 1), "tranvia": tipo_dia(dia, 4),
                         "l10": tipo_dia(dia, 10)},
            "resumen": salidas.resumen(filas), "salidas": filas}


@app.get("/api/health")
def health():
    con = _con()
    snap = _last_snapshot(con)
    n = con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
    return {"ok": True, "snapshots": n,
            "ultimo_ts": snap["ts"] if snap else None}


if WEB.exists():
    app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")

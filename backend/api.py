"""
API de Metroviolencia (FastAPI).

Endpoints:
    GET /api/network   -> geometría estática (líneas, estaciones, colores)
    GET /api/trains    -> trenes estimados del último snapshot
    GET /api/arrivals  -> llegadas crudas del último snapshot (por estación), con su desfase
    GET /api/cancelats -> trenes programados que no han pasado por una estación (?estacio=ID&dia=)
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

from . import historia, lectures, pasos, rutas, salidas, xat
from .calendario import tipo as tipo_dia
from .network import load_network
from .positions import estimar

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
WEB = ROOT / "web"
TOKEN_FILE = ROOT / "data" / "token.txt"
CLIENT_FILE = ROOT / "data" / "last_client.txt"  # señal de "hay cliente activo"
OPEN_FILE = ROOT / "data" / "open_until.txt"      # ventana temporal SIN token

app = FastAPI(title="Metro", version="0.1")
# la simulació d'un matí són ~10 MB de JSON: comprimit, ~1,5 MB
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=2000)


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
            # /metroviolencia; en metro.pr1.es cuelga de la raíz y su vhost de
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
    con = sqlite3.connect(DB, timeout=15)   # WAL: la lectura no espera; l'escriptura del xat, poc
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
    rows = [dict(zip(("estacion_id", "linea", "destino", "eta_seg", "eta_txt"), r[:5]))
            for r in lectures.de_pasada(con, snap["id"])]
    # desfase de cada llegada frente a su hora programada de paso (se ve al
    # tocar la estación); el horario y los sentidos se cargan UNA vez por pasada
    import datetime as dt
    net = _net()
    dia = dt.date.fromtimestamp(snap["ts"]).isoformat()
    try:
        prog = pasos._prog_dia(con, dia)
        dudosos = pasos.incoherentes(con, net, dia)
        sent = pasos.sentidos_inferidos(con, net, dia) if prog else {}
    except sqlite3.OperationalError:
        prog, dudosos, sent = {}, set(), {}
    por_est: dict[int, list] = {}
    for r in rows:
        ret = None
        if prog and r["eta_seg"] < salidas.HORIZONTE:   # "1 h" no tiene minutos
            ret = pasos.retraso_arribada(
                con, net, r["estacion_id"], r["linea"], r["destino"], snap["ts"] + r["eta_seg"],
                sent.get((r["linea"], r["destino"])), prog, dudosos)
        por_est.setdefault(r["estacion_id"], []).append(
            {"linea": r["linea"], "destino": r["destino"],
             "eta_seg": r["eta_seg"], "eta_txt": r["eta_txt"], "retraso": ret}
        )
    return {"ts": snap["ts"], "edad_seg": int(time.time()) - snap["ts"],
            "estaciones": por_est}


_canc_cache: dict[tuple[int, str], tuple[float, dict]] = {}


@app.get("/api/cancelats")
def cancelats(estacio: int, dia: str | None = None):
    """Trenes programados que NO han pasado por una estación en el día (regla:
    ni a su hora ni antes de la hora del siguiente). Se mantienen todo el día."""
    import datetime as dt
    dia = dia or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(dia)
    except ValueError:
        return JSONResponse({"error": "dia inválido"}, status_code=400)
    net = _net()
    if estacio not in net.estaciones:
        return JSONResponse({"error": "estación desconocida"}, status_code=400)
    c = _canc_cache.get((estacio, dia))
    if c and time.time() - c[0] < 60:
        return c[1]
    con = _con()
    try:
        lst = pasos.cancelados_estacion(con, net, estacio, dia)
    except sqlite3.OperationalError:
        lst = []
    res = {"dia": dia, "estacio": estacio, "ahora": int(time.time()), "cancelats": lst}
    for k in [k for k in _canc_cache if k[1] != dia]:
        _canc_cache.pop(k, None)              # no acumular días pasados
    _canc_cache[(estacio, dia)] = (time.time(), res)
    return res


_reg_cache: dict[tuple, tuple[int, dict]] = {}   # (tipus, id) -> (snapshot, resposta)


def _registre(clau: tuple, calcula) -> dict:
    """Respuesta de un registro del día, cacheada hasta la próxima pasada."""
    con = _con()
    snap = _last_snapshot(con)
    sid = snap["id"] if snap else 0
    c = _reg_cache.get(clau)
    if c and c[0] == sid:
        return c[1]
    try:
        files = calcula(con)
    except sqlite3.OperationalError:
        files = []
    res = {"ts": snap["ts"] if snap else None, "files": files}
    for k in [k for k, v in _reg_cache.items() if v[0] != sid]:
        _reg_cache.pop(k, None)               # solo se guarda la pasada actual
    _reg_cache[clau] = (sid, res)
    return res


@app.get("/api/registre_estacio")
def registre_estacio(estacio: int):
    """Pasos reales de hoy por una estación frente a su hora programada."""
    import datetime as dt
    net = _net()
    if estacio not in net.estaciones:
        return JSONResponse({"error": "estación desconocida"}, status_code=400)
    dia = dt.date.today().isoformat()
    return _registre(("estacio", estacio),
                     lambda con: pasos.registro_estacion(con, net, estacio, dia))


@app.get("/api/historia_tren")
def historia_tren(id: int):
    """Recorregut passat d'un tren (encara que ja no circule): posició a cada
    pasada, pas per estació frente a l'horari i com va acabar (viu / arribat /
    perdut a mitjan trajecte). Ver historia.py."""
    res = historia.historia(_con(), _net(), id)
    if res is None:
        return JSONResponse({"error": "no hi ha registre d'eixe tren"}, status_code=404)
    return res


@app.get("/api/registre_tren")
def registre_tren(id: int):
    """Paso por estación de un tren (track_id) hoy: horario frente a paso real."""
    return _registre(("tren", id), lambda con: pasos.registro_tren(con, _net(), id))


SIM_PREVI = 900   # s: la simulació comença 15 min abans del primer tren en marxa


@app.get("/api/simulacio")
def simulacio(dia: str | None = None, fins: str = "10:00"):
    """Totes les pasades d'un dia des de l'obertura (primer tren en marxa
    després de les 03:00, menys SIM_PREVI) fins a l'hora `fins`, amb els trens
    tal com els va veure el poller. La web les reprodueix com si fóra en
    directe. La taula `trenes` guarda 7 dies (purga.RETENER_TRENES)."""
    import datetime as dt
    try:
        d = dt.date.fromisoformat(dia) if dia else dt.date.today()
        hh, mm = (int(x) for x in fins.split(":"))
        t0 = int(time.mktime(dt.datetime(d.year, d.month, d.day, 3, 0).timetuple()))
        t1 = int(time.mktime(dt.datetime(d.year, d.month, d.day, hh, mm).timetuple()))
    except ValueError:
        return JSONResponse({"error": "dia o hora invàlids"}, status_code=400)
    con = _con()
    r = con.execute("SELECT MIN(ts) FROM trenes WHERE ts>=? AND ts<? AND origen=0",
                    (t0, t1)).fetchone()
    if not r or r[0] is None:
        return {"dia": d.isoformat(), "obertura": None, "pasades": []}
    obertura = r[0]
    pasades: dict[int, dict] = {}
    for sid, ts, tid, linea, destino, color, origen, wps, via in con.execute(
            "SELECT snapshot_id, ts, track_id, linea, destino, color, origen, waypoints, via "
            "FROM trenes WHERE ts>=? AND ts<=? ORDER BY snapshot_id",
            (obertura - SIM_PREVI, t1)):
        p = pasades.setdefault(sid, {"ts": ts, "trenes": []})
        p["trenes"].append({"id": tid, "linea": linea, "destino": destino, "color": color,
                            "origen": bool(origen), "waypoints": json.loads(wps),
                            "via": via or linea})
    return {"dia": d.isoformat(), "obertura": obertura, "fins": t1,
            "pasades": [pasades[k] for k in sorted(pasades)]}


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


MIN_ESTACIONES = 72    # pasada «buena»: la mitad de las estaciones leídas (poller.MIN_SEGUIMENT)
N_ESTACIONES = 144


def _pasadas(con):
    """(buena, última): la última pasada terminada con al menos MIN_ESTACIONES
    leídas, y la última terminada sin más. Si FGV no contesta, la web sigue
    enseñando la buena y avisa (06/11/2026: una pasada 0/144 vaciaba el mapa).
    Bases sin `estaciones_ok`: las dos son la de `_ultima_pasada`."""
    try:
        filas = con.execute("SELECT id AS snapshot_id, ts, estaciones_ok FROM snapshots "
                            "WHERE ts_fin IS NOT NULL ORDER BY id DESC LIMIT 60").fetchall()
    except sqlite3.OperationalError:
        r = _ultima_pasada(con)
        return r, r
    if not filas or con.execute("SELECT 1 FROM trenes LIMIT 1").fetchone() is None:
        r = _ultima_pasada(con)
        return r, r
    buena = next((f for f in filas if f["estaciones_ok"] is None
                  or f["estaciones_ok"] >= MIN_ESTACIONES), filas[0])
    return buena, filas[0]


def _degradat(buena, ultima) -> dict | None:
    """Aviso si la última pasada es degradada (aunque no quede ninguna buena)."""
    if ultima is None or "estaciones_ok" not in ultima.keys():
        return None
    ok = ultima["estaciones_ok"]
    if ok is None or ok >= MIN_ESTACIONES:
        return None
    return {"ts": ultima["ts"], "llegides": ok, "de": N_ESTACIONES,
            "mostrant": buena["ts"] if buena is not None and buena is not ultima else None}


def _ultima_pasada(con) -> sqlite3.Row | None:
    """Última pasada TERMINADA del poller (snapshot_id, ts), tenga trenes o no.
    Antes era la última con filas en `trenes`: de noche, sin trenes, el reloj
    «FGV» de la web contaba horas aunque el poller leyera cada 2 min. Bases sin
    ts_fin (pasadas antiguas, extractos de prueba): la última con trenes."""
    if con.execute("SELECT 1 FROM trenes LIMIT 1").fetchone() is None:
        return None                       # sin tracking aún: camino de reserva
    row = con.execute("SELECT id AS snapshot_id, ts FROM snapshots "
                      "WHERE ts_fin IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return row or con.execute(
        "SELECT snapshot_id, ts FROM trenes ORDER BY snapshot_id DESC LIMIT 1").fetchone()


@app.get("/api/ultimo")
def ultimo():
    """Marca de la última pasada servida (ts de inicio y ts_fin), para que el
    cliente se sincronice con el poller y pida /api/trains justo cuando hay
    datos nuevos, en vez de cada minuto a ciegas. Barato: se consulta cada 2 s."""
    _touch_client()
    con = _con()
    try:
        _buena, row = _pasadas(con)     # la última, buena o no: el cliente vuelve a pedir
    except sqlite3.OperationalError:    # /api/trains en cada pasada y se entera del aviso
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
        row, ultima = _pasadas(con)
    except sqlite3.OperationalError:
        row = ultima = None  # tabla aún no creada (poller viejo)
    if row is not None:
        snap_id, ts = row["snapshot_id"], row["ts"]
        trs = con.execute(
            "SELECT track_id,linea,destino,color,lat,lng,prox_estacion,eta_seg,"
            "origen,waypoints,via FROM trenes WHERE snapshot_id=?", (snap_id,)
        ).fetchall()
        trenes = [{
            "id": r["track_id"], "linea": r["linea"], "destino": r["destino"],
            "color": r["color"], "lat": r["lat"], "lng": r["lng"],
            "prox_estacion": r["prox_estacion"], "eta_seg": r["eta_seg"],
            "origen": bool(r["origen"]), "waypoints": json.loads(r["waypoints"]),
            # geometría del tren: la línea o su variante de ramal (L4: 41/42)
            "via": r["via"] or r["linea"],
        } for r in trs]
        # retraso en vivo frente al horario de paso (se ve al tocar el tren)
        # (i el de cada pròxima parada: la web mostra el de la primera que encara no ha passat)
        for t in trenes:
            try:
                t["retards"] = [] if t["origen"] else pasos.retrasos_vivos(con, _net(), t, ts)
            except sqlite3.OperationalError:
                t["retards"] = []
            t["retraso"] = t["retards"][0] if t["retards"] else None
        ok = row["estaciones_ok"] if "estaciones_ok" in row.keys() else None
        return {"ts": ts, "ts_fin": _ts_fin(con, snap_id), "edad_seg": int(time.time()) - ts,
                "srv_ms": int(time.time() * 1000),
                "n": len(trenes), "trenes": trenes, "noinfo": _noinfo(con, snap_id),
                "llegides": ok, "de": N_ESTACIONES, "degradat": _degradat(row, ultima)}

    # fallback (DB sin tracking aún): estima al vuelo, sin identidad persistente
    snap = _last_snapshot(con)
    if not snap:
        return {"ts": None, "trenes": []}
    llegadas = [r[:4] for r in lectures.de_pasada(con, snap["id"])]
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

    def _horari():
        # horario de paso y sentidos del día en memoria (~5 s en la Raspa): que no
        # los pague el primer popup que pida un registro
        try:
            con = _con()
            dia = dt.date.today().isoformat()
            pasos._prog_dia(con, dia)
            pasos.sentidos_inferidos(con, _net(), dia)
        except Exception:  # noqa: BLE001
            pass
    import threading
    threading.Thread(target=_horari, daemon=True).start()


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
        for r in lectures.de_pasada(con, snap["id"]):
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


# ---------------------------------------------------------------- enllaços
# metro.pr1.es/burjassot obri l'estació; metro.pr1.es/1234, el tren (o el seu
# recorregut si ja no circula). Les dues són la mateixa pàgina: la web llig el
# camí. Un nom que no és cap estació torna a l'inici.
from starlette.convertors import Convertor, register_url_convertor  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402


class _Enllac(Convertor):
    regex = "[a-z0-9]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("enllac", _Enllac())
_slugs_cache: dict[str, int] = {}


def _index():
    return FileResponse(WEB / "index.html")


@app.get("/", include_in_schema=False)
def inici():
    return _index()


@app.get("/{enllac:enllac}", include_in_schema=False)
def enllac(enllac: str):
    # una pàgina pròpia amb eixe nom (p. ex. /privacitat -> privacitat.html) mana
    if (WEB / f"{enllac}.html").is_file():
        return FileResponse(WEB / f"{enllac}.html")
    if (WEB / enllac / "index.html").is_file():
        return RedirectResponse(enllac + "/", status_code=301)
    if not _slugs_cache:
        _slugs_cache.update(historia.slugs(_net()))
    if enllac.isdigit() or enllac in _slugs_cache:
        return _index()
    return RedirectResponse("./", status_code=302)


@app.get("/{enllac:enllac}/", include_in_schema=False)
def enllac_barra(enllac: str):
    if (WEB / enllac / "index.html").is_file():
        return FileResponse(WEB / enllac / "index.html")
    return RedirectResponse("../" + enllac, status_code=301)


class _EnllacEscrit(Convertor):
    """Qualsevol tros sense barra ni punt (els fitxers estàtics en porten)."""
    regex = "[^/.]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("enllac_escrit", _EnllacEscrit())


@app.get("/{escrit:enllac_escrit}", include_in_schema=False)
def enllac_escrit(escrit: str):
    """Enllaç escrit a mà: metro.pr1.es/Burjassot o /Àngel Guimerà van a la
    forma normal (/burjassot, /angelguimera) en lloc de donar un 404."""
    s = historia.slug(escrit)
    if not _slugs_cache:
        _slugs_cache.update(historia.slugs(_net()))
    if s and s != escrit and (s.isdigit() or s in _slugs_cache or (WEB / f"{s}.html").is_file()):
        return RedirectResponse("./" + s, status_code=301)
    return RedirectResponse("./", status_code=302)


if WEB.exists():
    app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")

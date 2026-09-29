"""
Calculador de rutas: origen -> destino, con transbordos, y qué trenes EN VIVO
son los que hay que coger en cada tramo.

1) La ruta la calcula el planificador oficial de Metrovalencia (formulario
   `horarios-ruta`, el mismo que usa horarios.py): devuelve alternativas con
   sus pasos (De / Transbordo|Destino / tiempo / "Trenes con destino" / horas
   de salida). No dice la LÍNEA de cada paso: se deduce de la red (líneas que
   pasan por las dos estaciones del paso y que el tiempo real ve en la de
   subida).

2) Trenes de la ruta: para cada paso (subo en S, bajo en T, línea L) vale un
   tren en vivo de L si va en el sentido S->T, aún no ha pasado por S y su
   destino está en T o más allá. Todo sobre la distancia `d` de la línea
   comercial (la misma referencia que usan los waypoints de los trenes).
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from html import unescape

from . import horarios

MARGEN_D = 150       # m de tolerancia al comparar posiciones sobre la vía
TOKEN_TTL = 1800     # s que se reutiliza el auth_token del planificador
CACHE_TTL = 50       # s que se reutiliza una misma consulta (o, d)

_ALT_RE = re.compile(r'js-ruta-opcion')
_PASO_RE = re.compile(r'Paso\s*<span>\s*(\d+)\s*</span>(.*?)(?=Paso\s*<span>|$)', re.S)
_CAMPO_RE = re.compile(
    r'<p class="bg-gris mr10 nombre-campo">\s*([^<]+?)\s*</p>\s*<p>(.*?)</p>', re.S)
_MIN_RE = re.compile(r'(\d+)\s*minutos')
_DIRECTO_RE = re.compile(r'<p class="c-negro fs--14">\s*De\s+(.+?)\s+a\s+(.+?)\s*</p>', re.S)
_HORA_RE = re.compile(r'<p class="hora">(\d{1,2})</p>(.*?)</div>', re.S)
_MINUTO_RE = re.compile(r'<p class="minuto (?:par|impar)">(\d{1,2})</p>')

_lock = threading.Lock()
_token: tuple[str, float] | None = None
_cache: dict[tuple[int, int], tuple[float, list]] = {}


def _limpia(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _hms_a_min(s: str) -> int | None:
    m = re.match(r"(\d+):(\d+):(\d+)", s)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_alternativas(html: str) -> list[dict]:
    """HTML de horarios-ruta -> [{duracion_min, transbordos, pasos:[...]}]."""
    alts = []
    for bloque in _ALT_RE.split(html)[1:]:
        cab = _limpia(bloque.split("paso-horario", 1)[0])
        mdur = _MIN_RE.search(cab)
        mtr = re.search(r"(\d+)\s*transbordo", cab)
        pasos = []
        trozos = [c for _n, c in _PASO_RE.findall(bloque)]
        if not trozos:
            # trayecto DIRECTO: sin "Paso N"; origen y destino van en "De X a Y"
            mdir = _DIRECTO_RE.search(bloque)
            if mdir:
                trozos = [f'<p class="bg-gris mr10 nombre-campo">De</p><p>{mdir.group(1)}</p>'
                          f'<p class="bg-gris mr10 nombre-campo">Destino</p><p>{mdir.group(2)}</p>'
                          + bloque[mdir.end():]]
        for cuerpo in trozos:
            campos = {k.strip().lower(): _limpia(v) for k, v in _CAMPO_RE.findall(cuerpo)}
            salidas = [f"{int(h):02d}:{int(m):02d}"
                       for h, mins in _HORA_RE.findall(cuerpo)
                       for m in _MINUTO_RE.findall(mins)]
            fin = campos.get("transbordo") or campos.get("destino")
            pasos.append({
                "de": campos.get("de"),
                "a": fin,
                "transbordo": "transbordo" in campos,
                "tiempo_min": _hms_a_min(campos.get("tiempo estimado", "")),
                "destinos": [d.strip() for d in campos.get("trenes con destino", "").split(",")
                             if d.strip()],
                "salidas": salidas,
            })
        if pasos:
            alts.append({"duracion_min": int(mdur.group(1)) if mdur else None,
                         "transbordos": int(mtr.group(1)) if mtr else 0,
                         "pasos": pasos})
    return alts


def _get_token(session, headers, verify) -> str:
    global _token
    if _token and time.time() - _token[1] < TOKEN_TTL:
        return _token[0]
    tok = horarios._token(session, headers, verify)
    _token = (tok, time.time())
    return tok


def consultar_ruta(session, headers, verify, origen: int, destino: int) -> list[dict]:
    """Alternativas del planificador para salir desde ahora (ventana de 2 h)."""
    global _token
    with _lock:
        c = _cache.get((origen, destino))
        if c and time.time() - c[0] < CACHE_TTL:
            return c[1]
    ahora = dt.datetime.now()
    desde = (ahora - dt.timedelta(minutes=2)).strftime("%H:%M")
    hasta = min(ahora + dt.timedelta(hours=2),
                ahora.replace(hour=23, minute=59)).strftime("%H:%M")
    for intento in range(2):          # el token puede caducar: reintenta con uno nuevo
        tok = _get_token(session, headers, verify)
        data = (f"auth_token={tok}&action=horarios-ruta&origen={origen}"
                f"&destino={destino}&dia={ahora.date().isoformat()}"
                f"&horaDesde={desde}&horaHasta={hasta}")
        r = session.post(horarios.NOWP_URL, data={"action": "formularios_ajax", "data": data},
                         headers={**headers, "Referer": horarios.PLAN_URL}, timeout=30,
                         verify=verify)
        r.raise_for_status()
        j = r.json()
        alts = parse_alternativas(j.get("html") or "")
        if alts or intento:
            break
        _token = None
    with _lock:
        _cache[(origen, destino)] = (time.time(), alts)
    return alts


# ----------------------------------------------------------- líneas y trenes

def lineas_paso(net, s: int, t: int, vistas_en_s: set[int]) -> list[int]:
    """Líneas comerciales que pasan por S y por T. Si el tiempo real ve alguna
    de ellas en S, solo esas (descarta líneas que la red dibuja pero no paran)."""
    comunes = [lid for lid, lc in net.comerciales.items()
               if s in lc.estaciones and t in lc.estaciones]
    vistas = [l for l in comunes if l in vistas_en_s]
    return sorted(vistas or comunes)


def _sentido_tren(tren: dict, d_dest: float | None) -> int:
    wps = tren.get("waypoints") or []
    if len(wps) >= 2 and abs(wps[-1]["d"] - wps[0]["d"]) > 1:
        return 1 if wps[-1]["d"] > wps[0]["d"] else -1
    if d_dest is not None and wps:
        return 1 if d_dest > wps[0]["d"] else -1
    return 0


def trenes_paso(net, trenes: list[dict], linea: int, s: int, t: int,
                llegadas_s: list[tuple[int, str, int]]) -> list[dict]:
    """Trenes en vivo de `linea` que sirven para ir de S a T, ordenados por
    llegada a S, con su ETA a S (de los tiempos reales de la estación)."""
    lc = net.comerciales.get(linea)
    if not lc:
        return []
    d_s, d_t = lc.station_dist(s), lc.station_dist(t)
    if d_s is None or d_t is None or abs(d_t - d_s) < 1:
        return []
    sentido = 1 if d_t > d_s else -1
    buenos = []
    for tr in trenes:
        if tr["linea"] != linea or not tr.get("waypoints"):
            continue
        dest_id = net.id_por_nombre(tr["destino"])
        d_dest = lc.station_dist(dest_id) if dest_id is not None else None
        if _sentido_tren(tr, d_dest) != sentido:
            continue
        d_now = tr["waypoints"][0]["d"]
        if (d_s - d_now) * sentido < -MARGEN_D:
            continue                       # ya pasó por la estación de subida
        if d_dest is not None and (d_dest - d_t) * sentido < -MARGEN_D:
            continue                       # acaba antes de llegar a donde bajo
        buenos.append((abs(d_s - d_now), tr))
    buenos.sort(key=lambda x: x[0])
    # ETA a S: el k-ésimo tren más cercano de cada destino <-> k-ésima llegada
    # de ese destino en el panel de S
    etas: dict[str, list[int]] = {}
    for lin, dest, eta in llegadas_s:
        if lin == linea:
            etas.setdefault(dest, []).append(eta)
    for v in etas.values():
        v.sort()
    usados: dict[str, int] = {}
    out = []
    for _dist, tr in buenos:
        k = usados.get(tr["destino"], 0)
        usados[tr["destino"]] = k + 1
        lst = etas.get(tr["destino"], [])
        out.append({"id": tr["id"], "linea": linea, "destino": tr["destino"],
                    "eta_subida": lst[k] if k < len(lst) else None})
    return out


def enriquecer(net, alts: list[dict], trenes: list[dict],
               llegadas: dict[int, list[tuple[int, str, int]]]) -> list[dict]:
    """Añade ids de estación, líneas y trenes en vivo a cada paso."""
    for alt in alts:
        for p in alt["pasos"]:
            s, t = net.id_por_nombre(p["de"] or ""), net.id_por_nombre(p["a"] or "")
            p["de_id"], p["a_id"] = s, t
            if s is None or t is None:
                p["lineas"], p["trenes"] = [], []
                continue
            vistas = {lin for lin, _d, _e in llegadas.get(s, [])}
            p["lineas"] = lineas_paso(net, s, t, vistas)
            p["trenes"] = [x for lin in p["lineas"]
                           for x in trenes_paso(net, trenes, lin, s, t, llegadas.get(s, []))]
            p["trenes"].sort(key=lambda x: (x["eta_subida"] is None, x["eta_subida"] or 0))
    return alts

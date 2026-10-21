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
ETA_PRECISA = 3600   # s: desde aquí la web da "1 h"/"2 h" sin minutos
VENTANA_CANC = 1800  # s: horizonte en el que se avisa de salidas sin tren anunciado
VENTANA_CASE = 900   # s: un tren en vivo se casa con una salida programada a <15 min

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


def _pedir(session, headers, verify, origen, destino, dia, desde, hasta) -> list[dict]:
    global _token
    for intento in range(2):          # el token puede caducar: reintenta con uno nuevo
        tok = _get_token(session, headers, verify)
        data = (f"auth_token={tok}&action=horarios-ruta&origen={origen}"
                f"&destino={destino}&dia={dia}&horaDesde={desde}&horaHasta={hasta}")
        r = session.post(horarios.NOWP_URL, data={"action": "formularios_ajax", "data": data},
                         headers={**headers, "Referer": horarios.PLAN_URL}, timeout=30,
                         verify=verify)
        r.raise_for_status()
        alts = parse_alternativas(r.json().get("html") or "")
        if alts or intento:
            return alts
        _token = None
    return []


def _con_salidas(alts: list[dict]) -> bool:
    return any(a["pasos"] and a["pasos"][0]["salidas"] for a in alts)


def consultar_ruta(session, headers, verify, origen: int, destino: int) -> dict:
    """Alternativas del planificador para salir desde ahora. Si en las próximas
    2 h no sale ningún tren, amplía al resto del día y, si tampoco, a mañana:
    así siempre se sabe CUÁNDO habrá tren. Devuelve {dia, ventana, alternativas}."""
    with _lock:
        c = _cache.get((origen, destino))
        if c and time.time() - c[0] < CACHE_TTL:
            return c[1]
    ahora = dt.datetime.now()
    hoy = ahora.date().isoformat()
    # 20 min hacia atrás: un tren que va tarde aún tiene que casar con su hora
    desde = max(ahora - dt.timedelta(minutes=20),
                ahora.replace(hour=0, minute=0)).strftime("%H:%M")
    fin_2h = ahora + dt.timedelta(hours=2)
    hasta = "23:59" if fin_2h.date() != ahora.date() else fin_2h.strftime("%H:%M")
    res = {"dia": hoy, "ventana": "2h",
           "alternativas": _pedir(session, headers, verify, origen, destino, hoy, desde, hasta)}
    if not _con_salidas(res["alternativas"]) and hasta != "23:59":
        res = {"dia": hoy, "ventana": "hoy",
               "alternativas": _pedir(session, headers, verify, origen, destino, hoy, desde, "23:59")}
    if not _con_salidas(res["alternativas"]):
        manana = (ahora.date() + dt.timedelta(days=1)).isoformat()
        res = {"dia": manana, "ventana": "manana",
               "alternativas": _pedir(session, headers, verify, origen, destino,
                                      manana, "00:00", "23:59")}
    with _lock:
        _cache[(origen, destino)] = (time.time(), res)
    return res


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
                llegadas_s: list[tuple[int, str, int]], destinos_validos: list[str] | None = None) -> list[dict]:
    """Trenes en vivo de `linea` que sirven para ir de S a T, ordenados por
    llegada a S, con su ETA a S (de los tiempos reales de la estación).
    `destinos_validos`: "Trenes con destino" del planificador para este paso."""
    lc = net.comerciales.get(linea)
    if not lc:
        return []
    d_s, d_t = lc.station_dist(s), lc.station_dist(t)
    if d_s is None or d_t is None or abs(d_t - d_s) < 1:
        return []
    sentido = 1 if d_t > d_s else -1
    # solo se filtra si todos los nombres del planificador existen en la red:
    # si alguno no casa, filtrar podría vaciar la lista de trenes buenos
    validos = {d.strip().lower() for d in destinos_validos or []}
    if any(net.id_por_nombre(d.strip()) is None for d in destinos_validos or []):
        validos = set()
    buenos = []
    for tr in trenes:
        if tr["linea"] != linea or not tr.get("waypoints"):
            continue
        # En líneas con ramales (L4) la distancia sobre una sola polyline no
        # distingue ramas: manda la lista de destinos del planificador oficial.
        if validos and tr["destino"].strip().lower() not in validos:
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


def _ts(dia: str, hhmm: str) -> int:
    return int(time.mktime(dt.datetime.strptime(f"{dia} {hhmm}", "%Y-%m-%d %H:%M").timetuple()))


def desfases(trenes_p: list[dict], salidas: list[str], dia: str, ts_datos: int) -> None:
    """Casa cada tren en vivo (hora real prevista en la estación de subida =
    ts_datos + eta) con su salida PROGRAMADA más cercana (±VENTANA_CASE) y
    anota hora_prog y desfase_s (+ retraso, − adelanto)."""
    prog = sorted(_ts(dia, h) for h in salidas)
    usadas: set[int] = set()
    for t in sorted(trenes_p, key=lambda x: (x["eta_subida"] is None, x["eta_subida"] or 0)):
        t["hora_prog"] = t["desfase_s"] = None
        if t["eta_subida"] is None or t["eta_subida"] >= ETA_PRECISA:
            continue          # "1 h" no trae minutos: no sirve para medir desfase
        real = ts_datos + t["eta_subida"]
        cands = [p for p in prog if p not in usadas and abs(real - p) <= VENTANA_CASE]
        if not cands:
            continue
        p = min(cands, key=lambda x: abs(real - x))
        usadas.add(p)
        t["hora_prog"] = dt.datetime.fromtimestamp(p).strftime("%H:%M")
        t["desfase_s"] = real - p


def sin_tren(p: dict, llegadas_s: list[tuple[int, str, int]], dia: str,
             ts_datos: int, ahora: float) -> list[str]:
    """Salidas PROGRAMADAS en S de los próximos VENTANA_CANC s que no casan con
    ninguna llegada anunciada en el panel de S (misma línea y destino válido):
    posible cancelación. Solo se juzga si el panel ya anuncia algún tren
    POSTERIOR a esa hora (si no, puede que simplemente aún no se vea)."""
    dest_ok = {t["destino"] for t in p["trenes"]} | set(p.get("destinos") or [])
    if not dest_ok:
        return []
    anun = sorted(ts_datos + eta for lin, dest, eta in llegadas_s
                  if lin in p["lineas"] and dest in dest_ok and eta < ETA_PRECISA)
    if not anun:
        return []
    usadas: set[int] = set()
    faltan = []
    for h in p["salidas"]:
        tp = _ts(dia, h)
        if not (ahora - 120 <= tp <= ahora + VENTANA_CANC) or tp >= anun[-1]:
            continue
        cands = [i for i, a in enumerate(anun) if i not in usadas and abs(a - tp) <= VENTANA_CASE]
        if cands:
            usadas.add(min(cands, key=lambda i: abs(anun[i] - tp)))
        else:
            faltan.append(h)
    return faltan


def enriquecer(net, alts: list[dict], trenes: list[dict],
               llegadas: dict[int, list[tuple[int, str, int]]],
               dia: str | None = None, ts_datos: int | None = None) -> list[dict]:
    """Añade ids de estación, líneas, trenes en vivo (con desfase frente al
    horario) y la próxima salida programada a cada paso."""
    ahora = time.time()
    for alt in alts:
        for p in alt["pasos"]:
            s, t = net.id_por_nombre(p["de"] or ""), net.id_por_nombre(p["a"] or "")
            p["de_id"], p["a_id"] = s, t
            futuras = [h for h in p["salidas"] if dia and _ts(dia, h) >= ahora - 60]
            p["proxima_prog"] = futuras[0] if futuras else None
            if s is None or t is None:
                p["lineas"], p["trenes"] = [], []
                continue
            vistas = {lin for lin, _d, _e in llegadas.get(s, [])}
            p["lineas"] = lineas_paso(net, s, t, vistas)
            p["trenes"] = [x for lin in p["lineas"]
                           for x in trenes_paso(net, trenes, lin, s, t, llegadas.get(s, []),
                                               p.get("destinos"))]
            p["trenes"].sort(key=lambda x: (x["eta_subida"] is None, x["eta_subida"] or 0))
            if dia and ts_datos:
                desfases(p["trenes"], p["salidas"], dia, ts_datos)
                p["sin_tren"] = sin_tren(p, llegadas.get(s, []), dia, ts_datos, ahora)
    return alts

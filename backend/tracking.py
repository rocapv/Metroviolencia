"""
Seguimiento de trenes físicos entre pulls (v1).

La fuente de Metrovalencia NO numera los trenes: cada pull da "próximas
llegadas por estación" y el estimador (positions.py) reconstruye los trenes
físicos de ese instante, pero sin identidad entre un pull y el siguiente.

Este módulo asigna un `track_id` PERSISTENTE a cada tren físico y lo sigue de
un snapshot al siguiente, con esta idea:

  Para cada tren del snapshot ANTERIOR, se predice dónde estaría AHORA avanzando
  su trayectoria (waypoints d/eta) el tiempo transcurrido Δt. Ese punto previsto
  se casa con el tren ACTUAL más próximo de la misma (línea, destino) —mismo
  sentido—, respetando el ORDEN a lo largo de la ruta (alineamiento tipo DP).
  Los que casan heredan el track_id; los que aparecen reciben uno nuevo; los que
  desaparecen se dan de baja (no se emiten).

Con pull de 1 min el emparejamiento es directo; a 5 min hay más recorrido entre
muestras, por eso la puerta de distancia (GATE) crece con Δt.
"""
from __future__ import annotations

from collections import defaultdict


def predict_d(waypoints: list, tau: float):
    """Distancia sobre la vía prevista tras `tau` s, interpolando los waypoints
    (mismo criterio que el frontend distVivaTren). None si no hay waypoints."""
    wp = waypoints
    if not wp:
        return None
    if tau <= wp[0]["eta"]:
        return wp[0]["d"]
    for i in range(len(wp) - 1):
        if tau <= wp[i + 1]["eta"]:
            dt = wp[i + 1]["eta"] - wp[i]["eta"]
            f = 1.0 if dt <= 0 else (tau - wp[i]["eta"]) / dt
            return wp[i]["d"] + f * (wp[i + 1]["d"] - wp[i]["d"])
    return wp[-1]["d"]   # ha llegado al final de su trayectoria conocida


def _tol(dt: float) -> float:
    """Tolerancia (m) sobre el intervalo plausible: cubre el ruido del
    estimador y el error de predicción (NO el desplazamiento del tren, que ya
    está dentro del intervalo). Crece con Δt.

    03/11/2026: era 200 + 0,5·Δt (260 m a 120 s) y, con la pasada cada 120 s,
    el mismo tren caía fuera 1.245 veces en 6 h y recibía id nuevo (2.244 ids
    para ~100 trenes). Reproducido sobre esas 6 h: 1000 + 8·Δt (1.960 m a
    120 s) deja 928 ids, de vida mediana 14 pasadas en vez de 4. No cruza
    trenes: _alinear casa en orden sobre la vía, y dos trenes de la misma
    línea y destino van a varios km."""
    return 1000.0 + 8.0 * max(0.0, dt)


def _coste(lo: float, hi: float, cd: float) -> float:
    """Distancia del punto actual `cd` al intervalo plausible [lo,hi] (0 dentro).
    El tren pudo moverse entre 0 y v·Δt según cuándo refrescó la fuente, así que
    su posición actual debería caer dentro de ese intervalo."""
    if cd < lo:
        return lo - cd
    if cd > hi:
        return cd - hi
    return 0.0


def _alinear(pred_rangos: list, curr_d: list, dt: float, compat=None) -> list:
    """Alineamiento óptimo que preserva el orden entre los INTERVALOS previstos
    de los trenes anteriores (`pred_rangos` = lista de (lo,hi)) y las posiciones
    actuales (`curr_d`), minimizando la suma de distancias-al-intervalo de los
    emparejados. Devuelve una lista del tamaño de curr_d con el índice de
    `pred_rangos` casado, o None si es un tren nuevo.

    DP tipo alineamiento de secuencias: casar cuesta la distancia-al-intervalo
    (si <= TOL); saltar (tren que se va / tren nuevo) cuesta TOL. Se prefiere
    casar siempre que caiga dentro de la tolerancia y no rompa el orden."""
    n, m = len(pred_rangos), len(curr_d)
    tol = _tol(dt)
    INF = float("inf")

    def cm(i, j):  # coste de casar pred_rangos[i] con curr_d[j]
        if compat is not None and not compat(i, j):
            return INF
        c = _coste(pred_rangos[i][0], pred_rangos[i][1], curr_d[j])
        return c if c <= tol else INF

    # dp[i][j] = coste mínimo alineando pred[:i] con curr[:j]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + tol        # todos los previstos sin casar
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + tol        # todos los actuales sin casar
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = min(dp[i - 1][j - 1] + cm(i - 1, j - 1),
                           dp[i - 1][j] + tol,
                           dp[i][j - 1] + tol)
    # backtrack
    asig = [None] * m
    i, j = n, m
    while i > 0 and j > 0:
        match = dp[i - 1][j - 1] + cm(i - 1, j - 1)
        if match <= dp[i - 1][j] + tol and match <= dp[i][j - 1] + tol and match < INF:
            asig[j - 1] = i - 1
            i -= 1
            j -= 1
        elif dp[i - 1][j] + tol <= dp[i][j - 1] + tol:
            i -= 1
        else:
            j -= 1
    return asig


# 04/11/2026: un tren «en espera» en cabecera es la PRÓXIMA EIXIDA que anuncia
# FGV (el panel de la capçalera només en dona una per destí), no un tren físic.
# Quan el tren se n'anava, la capçalera passava a anunciar l'eixida següent
# (10-15 min després) i el seguiment casava l'id amb eixa eixida nova en lloc
# del tren que eixia: a les capçaleres de final de via (d alta) l'empat el
# guanyava sempre l'espera, i si FGV donava l'eixida tard, també a les altres.
# El tren que eixia rebia un número nou i l'«espera» vivia hores amb el mateix
# (03/11: 49 ids més d'1 h, un 16 h a Machado): el torn de cada unitat es
# tallava a cada viatge. Ara dues esperes només són el mateix tren si anuncien
# la MATEIXA eixida (± SALIDA_TOL).
SALIDA_TOL = 300   # s


def track(curr: list, prev: list, dt: float, next_id: int, vias: dict | None = None):
    """Asigna track_id a cada tren actual.

    curr: lista de objetos Tren (positions.estimar) — tienen .linea, .destino y
          .waypoints (el primer waypoint es la posición actual, d con eta 0).
    prev: lista de dicts del snapshot anterior con
          {track_id, linea, destino, waypoints[, origen, eta]} (origen/eta: si
          esperaba en capçalera i quan n'havia d'eixir; veure SALIDA_TOL).
    dt:   segundos entre el snapshot anterior y el actual.
    next_id: primer id libre (max(track_id)+1).

    vias: {via: (linea, d_union)} de las variantes de ramal (network.vias_de). Un
          tren solo puede casar con otro de la MISMA vía, o con uno de otra vía
          si los dos están en el tronco común (d >= d_union de ambas): un tren
          en el ramal de Terramelar no es el que va por el ramal de Mas del
          Rosari aunque sus d coincidan. Sin `vias`, se ignora la vía.

    Devuelve (ids, next_id): ids[i] = track_id del tren curr[i]."""
    ids = [None] * len(curr)
    vias = vias or {}

    def grupos_de(linea, destino, via, d):
        """Grupos de emparejamiento a los que pertenece un tren: su vía y, si
        está en el tronco, también las demás vías de su línea."""
        if not vias:
            return [(linea, destino, 0)]
        propia = vias.get(via, (linea, 0.0))[1]
        out = [(linea, destino, via)]
        if d >= propia:
            for v, (l, du) in vias.items():
                if l == linea and v != via and d >= du:
                    out.append((linea, destino, v))
            if via != linea and (linea not in vias):
                out.append((linea, destino, linea))
        return out

    prev_by = defaultdict(list)
    for p in prev:
        wp = p.get("waypoints") or []
        d0 = wp[0]["d"] if wp else 0.0
        for g in grupos_de(p["linea"], p["destino"], p.get("via") or p["linea"], d0):
            prev_by[g].append(p)

    curr_by = defaultdict(list)
    for i, t in enumerate(curr):
        for g in grupos_de(t.linea, t.destino, getattr(t, "via", 0) or t.linea, t.waypoints[0]["d"]):
            curr_by[g].append(i)

    usados: set[int] = set()     # track_id ya asignados (un tren puede estar en varios grupos)
    # la vía principal primero: en el tronco decide ella; las variantes recogen lo del ramal
    for key in sorted(curr_by, key=lambda k: (k[2] != 0 and k[2] != k[0], k)):
        cidx = [i for i in curr_by[key] if ids[i] is None]
        if not cidx:
            continue
        P = [p for p in prev_by.get(key, []) if p["track_id"] not in usados]
        # previstos: intervalo plausible [d_viejo, d_predicho] + track_id.
        # El tren pudo moverse entre 0 (fuente sin refrescar) y v·Δt (refresco
        # completo) desde el pull anterior; su posición actual cae en ese rango.
        pred_entries = []
        for p in P:
            wp = p.get("waypoints") or []
            if not wp:
                continue
            d0 = wp[0]["d"]
            pd = predict_d(wp, dt)
            if pd is None:
                pd = d0
            lo, hi = (d0, pd) if d0 <= pd else (pd, d0)
            # eixida anunciada, en segons des d'ara (només si esperava en capçalera)
            sal = (p.get("eta") or 0) - dt if p.get("origen") else None
            pred_entries.append((lo, hi, p["track_id"], sal))
        pred_entries.sort(key=lambda e: (e[0] + e[1]) / 2.0)
        # actuales de este grupo, ordenados por d (posición actual)
        cidx_sorted = sorted(cidx, key=lambda i: curr[i].waypoints[0]["d"])
        pred_rangos = [(e[0], e[1]) for e in pred_entries]
        curr_d = [curr[i].waypoints[0]["d"] for i in cidx_sorted]

        def compat(pi, k, cidx_sorted=cidx_sorted, pred_entries=pred_entries):
            sal_p = pred_entries[pi][3]
            t = curr[cidx_sorted[k]]
            if sal_p is None or not getattr(t, "origen", False):
                return True
            return abs((getattr(t, "eta_seg", 0) or 0) - sal_p) <= SALIDA_TOL

        asig = _alinear(pred_rangos, curr_d, dt, compat)
        for k, i in enumerate(cidx_sorted):
            pi = asig[k]
            if pi is not None:
                ids[i] = pred_entries[pi][2]
                usados.add(ids[i])

    for i in range(len(curr)):
        if ids[i] is None:
            ids[i] = next_id
            next_id += 1
    return ids, next_id


def herencias(acaban: list, esperan: list) -> dict:
    """Un tren que da la vuelta en cabecera es el MISMO tren: conserva su número.

    FGV anuncia la próxima salida de una cabecera mucho antes de que llegue el
    tren que la hará, así que ese tren «en espera» ya tiene id propio mientras
    el que llega aún circula. Cuando el que llega ACABA su viaje en S, el que
    espera en S pasa a llevar su número.

    acaban:  trenes que esta pasada dejaron de verse llegando a su destino S:
             [{track_id, linea, destino}].
    esperan: trenes en espera (origen) que solo han estado en espera (aún no
             han heredado ni circulado): [{track_id, linea, cabecera, eta}].

    Prioridad: misma línea; si no, cualquiera (en València Sud o Empalme un
    tren sigue con otra línea). Entre los que esperan, el que sale antes.
    Devuelve {id_en_espera: id_heredado}."""
    libres = sorted(esperan, key=lambda e: e["eta"])
    out = {}
    for a in acaban:
        en = [e for e in libres if e["cabecera"] == a["destino"]]
        e = next((e for e in en if e["linea"] == a["linea"]), en[0] if en else None)
        if e is not None:
            out[e["track_id"]] = a["track_id"]
            libres.remove(e)
    return out


ACABA_M = 4000   # m: a menos de esto de su destino, un tren que deja de verse ha acabado el viaje


# ------------------------------------------------------------ relleu en capçalera
# 04/11/2026, per a l'inventari de flota: `herencias` només mirava els trens en
# espera en capçalera EN LA MATEIXA pasada en què arribava l'altre. Reproduït el
# 03/11: de 1.786 arribades, 816 no passaven el número (no hi havia cap espera
# en eixe moment: Marítim 208, Vicent Andrés Estellés 76, Empalme 74...), i
# 1.291 ids naixien ja circulant (el tren que ix apareix directament eixint de
# la capçalera). Ara el que arriba espera fins a RELLEU_S el pròxim que naix en
# eixa capçalera i se'n va (en ordre d'arribada). Un tren que ix sense ningú
# esperant ve de cotxeres (comença torn); un que arriba i ningú no el relleva,
# se'n va a cotxeres (acaba torn).
RELLEU_S = 1500   # s: temps màxim de volta en capçalera
RELLEU_M = 2500   # m: el que naix circulant ha d'estar a menys d'açò de la capçalera


def relleus(net, pendents: list, nous: list) -> dict:
    """pendents: [{track_id, linea, destino, ts}] trens que han acabat el
             viatge (arribat al destí) fa menys de RELLEU_S i que ningú no ha
             rellevat, en qualsevol ordre.
    nous:    [{track_id, linea, destino, via, d, dir, origen, cabecera, prox}] trens que poden
             ser el mateix que ix: en espera en capçalera i sense haver circulat
             mai (cabecera = nom de l'estació), o nascuts en esta pasada ja
             circulant a menys de RELLEU_M de la capçalera i allunyant-se'n.
    Per cada capçalera, el primer que va arribar és el primer que ix; dins
    d'un mateix moment, la mateixa línia primer. Torna {id_nou: id_pendent}."""
    out, usats = {}, set()
    for p in sorted(pendents, key=lambda p: p["ts"]):
        sid = net.id_por_nombre(p["destino"])
        if sid is None:
            continue
        cands = []
        for n in nous:
            if n["track_id"] in usats:
                continue
            if n.get("destino") == p["destino"]:
                continue        # va CAP a la capçalera (un que arriba amb número nou), no n'ix
            if n.get("origen"):
                if n.get("cabecera") != net.estaciones[sid].nombre:
                    continue
                dist = 0.0
            else:
                lc = net.comerciales.get(n["via"]) or net.comerciales.get(n["linea"])
                d_cab = next((d for d, s in lc.orden if s == sid), None) if lc else None
                if d_cab is None:
                    continue
                dist = (n["d"] - d_cab) * n["dir"]       # >0: s'allunya de la capçalera
                # Si la pròxima parada és la mateixa capçalera, l'estimador l'ha
                # posat DARRERE (a Seminari el que ix cap a Torrent comparteix
                # destí amb els que venen de Bétera i es dibuixa a 2,5 km, «venint»
                # de l'estació anterior): també val.
                darrere = -RELLEU_M - 500 if n.get("prox") == net.estaciones[sid].nombre else -200
                if not (darrere <= dist <= RELLEU_M):
                    continue
            cands.append((n["linea"] != p["linea"], abs(dist), n["track_id"]))
        if cands:
            n_id = min(cands)[2]
            out[n_id] = p["track_id"]
            usats.add(n_id)
    return out


def llega_a_destino(net, linea: int, via: int, destino: str, waypoints: list) -> bool:
    """¿Estaba el tren a menos de ACABA_M de su destino sobre su vía? La
    cabecera solo anuncia SALIDAS: el tren que llega deja de verse en la
    penúltima estación, no en la última."""
    if not waypoints:
        return False
    sid = net.id_por_nombre(destino)
    lc = net.comerciales.get(via) or net.comerciales.get(linea)
    if sid is None or lc is None:
        return False
    d_dest = next((d for d, s in lc.orden if s == sid), None)
    return d_dest is not None and abs(d_dest - waypoints[0]["d"]) <= ACABA_M


# ------------------------------------------------------------ rescat a mitja línia
# 04/11/2026: amb la tolerància nova encara morien trens a mitjan trajecte (un
# salt de l'estimador, una lectura fallida d'una estació) i tornaven en la
# pasada següent amb un número nou: 1.388 ids en 12 h per a ~100 trens, i el
# recorregut d'un tren «perdut» semblava una cancel·lació que no ho era.
# Reproduït sobre 12 h: dels que «acabaven perduts», 127 seguien a prop amb un
# ALTRE destí rotulat (FGV canvia el cartell a mitjan recorregut: un L1 «València
# Sud» passa a «Castelló») i 51 tornaven al cap de més de 7 min.
RESCAT_S = 1800       # s: un tren perdut fa menys d'açò encara es pot retrobar. 900 -> 1800
                      # el 04/11/2026: reproduït el 03/11, 887 -> 789 ids i 620 -> 522 perduts
                      # a mitja línia (amb 4.000 m d'avanç n'eixien 752, però el risc
                      # d'ajuntar dos trens creix i no compensa)
RESCAT_ENRERE = 500   # m que pot aparéixer per darrere d'on es va perdre (soroll)
RESCAT_AVANT = 2500   # m que pot aparéixer més enllà d'on havia d'estar ara
RESCAT_ALTRE = 1500   # m al voltant d'on havia d'estar si ara porta un altre destí


def d_previst(waypoints: list, tau: float) -> float:
    """Posició sobre la via `tau` segons després de la pasada dels waypoints."""
    wp = waypoints
    if tau <= wp[0]["eta"]:
        return wp[0]["d"]
    for a, b in zip(wp, wp[1:]):
        if tau <= b["eta"]:
            dt = b["eta"] - a["eta"]
            f = 1.0 if dt <= 0 else (tau - a["eta"]) / dt
            return a["d"] + f * (b["d"] - a["d"])
    return wp[-1]["d"]


def _vies_compatibles(via_a: int, via_b: int, d_a: float, d_b: float, vias: dict) -> bool:
    if via_a == via_b:
        return True
    du = max(vias.get(via_a, (0, 0.0))[1], vias.get(via_b, (0, 0.0))[1])
    return min(d_a, d_b) >= du          # els dos al tronc comú


def rescats(perduts: list, nous: list, ara: int, vias: dict | None = None) -> dict:
    """Un tren que deixa de veure's LLUNY del seu destí no s'ha esfumat: si en
    esta pasada apareix un tren NOU de la mateixa línia, destí i via prop d'on
    havia d'estar, és ell i conserva el número.

    perduts: [{track_id, linea, destino, via, ts, waypoints}] — perduts fa
             menys de RESCAT_S, a mitjan trajecte (no a la capçalera).
    nous:    [{track_id, linea, destino, via, d, dir}] — ids estrenats en esta
             pasada que no estan en capçalera (dir = sentit sobre la via, ±1).
    Amb un altre destí rotulat només val si va en el mateix sentit i a menys
    de RESCAT_ALTRE d'on havia d'estar. Primer els del mateix destí; dins de
    cada grup, el més pròxim a la posició prevista; un a un.
    Devuelve {nou: perdut}."""
    vias = vias or {}
    parelles = []
    for p in perduts:
        wps = p.get("waypoints") or []
        if not wps:
            continue
        d0 = wps[0]["d"]
        dir_ = 1 if wps[-1]["d"] >= d0 else -1
        s_pred = (d_previst(wps, ara - p["ts"]) - d0) * dir_
        for c in nous:
            if c["linea"] != p["linea"]:
                continue
            if not _vies_compatibles(p["via"], c["via"], d0, c["d"], vias):
                continue
            s = (c["d"] - d0) * dir_
            if c["destino"] == p["destino"]:
                if -RESCAT_ENRERE <= s <= s_pred + RESCAT_AVANT:
                    parelles.append((0, abs(s - s_pred), c["track_id"], p["track_id"]))
            elif c.get("dir") == dir_ and abs(s - s_pred) <= RESCAT_ALTRE:
                parelles.append((1, abs(s - s_pred), c["track_id"], p["track_id"]))
    out, nous_usats, perduts_usats = {}, set(), set()
    for _, _, n, p in sorted(parelles):
        if n in nous_usats or p in perduts_usats:
            continue
        out[n] = p
        nous_usats.add(n)
        perduts_usats.add(p)
    return out

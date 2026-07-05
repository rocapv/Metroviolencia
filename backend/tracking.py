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
    """Tolerancia (m) sobre el intervalo plausible: sólo cubre el ruido del
    estimador y el error de predicción (NO el desplazamiento del tren, que ya
    está dentro del intervalo). Crece un poco con Δt."""
    return 200.0 + 0.5 * max(0.0, dt)


def _coste(lo: float, hi: float, cd: float) -> float:
    """Distancia del punto actual `cd` al intervalo plausible [lo,hi] (0 dentro).
    El tren pudo moverse entre 0 y v·Δt según cuándo refrescó la fuente, así que
    su posición actual debería caer dentro de ese intervalo."""
    if cd < lo:
        return lo - cd
    if cd > hi:
        return cd - hi
    return 0.0


def _alinear(pred_rangos: list, curr_d: list, dt: float) -> list:
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


def track(curr: list, prev: list, dt: float, next_id: int):
    """Asigna track_id a cada tren actual.

    curr: lista de objetos Tren (positions.estimar) — tienen .linea, .destino y
          .waypoints (el primer waypoint es la posición actual, d con eta 0).
    prev: lista de dicts del snapshot anterior con
          {track_id, linea, destino, waypoints}.
    dt:   segundos entre el snapshot anterior y el actual.
    next_id: primer id libre (max(track_id)+1).

    Devuelve (ids, next_id): ids[i] = track_id del tren curr[i]."""
    ids = [None] * len(curr)

    prev_by = defaultdict(list)
    for p in prev:
        prev_by[(p["linea"], p["destino"])].append(p)

    curr_by = defaultdict(list)
    for i, t in enumerate(curr):
        curr_by[(t.linea, t.destino)].append(i)

    for key, cidx in curr_by.items():
        P = prev_by.get(key, [])
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
            pred_entries.append((lo, hi, p["track_id"]))
        pred_entries.sort(key=lambda e: (e[0] + e[1]) / 2.0)
        # actuales de este grupo, ordenados por d (posición actual)
        cidx_sorted = sorted(cidx, key=lambda i: curr[i].waypoints[0]["d"])
        pred_rangos = [(e[0], e[1]) for e in pred_entries]
        curr_d = [curr[i].waypoints[0]["d"] for i in cidx_sorted]
        asig = _alinear(pred_rangos, curr_d, dt)
        for k, i in enumerate(cidx_sorted):
            pi = asig[k]
            if pi is not None:
                ids[i] = pred_entries[pi][2]

    for i in range(len(curr)):
        if ids[i] is None:
            ids[i] = next_id
            next_id += 1
    return ids, next_id

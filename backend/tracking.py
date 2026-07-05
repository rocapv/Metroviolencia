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


def _gate(dt: float) -> float:
    """Puerta de distancia (m) para aceptar un emparejamiento: crece con el
    intervalo entre pulls (a 1 min ~580 m; a 5 min ~1300 m)."""
    return 400.0 + 3.0 * max(0.0, dt)


def _alinear(pred: list, curr_d: list, dt: float) -> list:
    """Alineamiento óptimo que preserva el orden entre trenes previstos (`pred`,
    lista de distancias previstas) y actuales (`curr_d`, distancias actuales),
    minimizando la suma de |pred-actual| de los emparejados. Devuelve una lista
    del tamaño de curr_d con el índice de `pred` casado, o None si es nuevo.

    DP tipo alineamiento de secuencias: casar cuesta la distancia (si <= GATE);
    saltar (tren que se va / tren nuevo) cuesta GATE. Así se prefiere casar
    siempre que caiga dentro de la puerta y no rompa el orden."""
    n, m = len(pred), len(curr_d)
    gate = _gate(dt)
    INF = float("inf")
    # dp[i][j] = coste mínimo alineando pred[:i] con curr[:j]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gate       # todos los previstos sin casar
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gate       # todos los actuales sin casar
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost_match = abs(pred[i - 1] - curr_d[j - 1])
            match = dp[i - 1][j - 1] + (cost_match if cost_match <= gate else INF)
            skip_p = dp[i - 1][j] + gate
            skip_c = dp[i][j - 1] + gate
            dp[i][j] = min(match, skip_p, skip_c)
    # backtrack
    asig = [None] * m
    i, j = n, m
    while i > 0 and j > 0:
        cost_match = abs(pred[i - 1] - curr_d[j - 1])
        match = dp[i - 1][j - 1] + (cost_match if cost_match <= gate else INF)
        if match <= dp[i - 1][j] + gate and match <= dp[i][j - 1] + gate and match < INF:
            asig[j - 1] = i - 1
            i -= 1
            j -= 1
        elif dp[i - 1][j] + gate <= dp[i][j - 1] + gate:
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
        # previstos: (d_previsto, track_id), ordenados por d
        pred_pairs = []
        for p in P:
            pd = predict_d(p["waypoints"], dt)
            if pd is not None:
                pred_pairs.append((pd, p["track_id"]))
        pred_pairs.sort(key=lambda x: x[0])
        # actuales de este grupo, ordenados por d (posición actual)
        cidx_sorted = sorted(cidx, key=lambda i: curr[i].waypoints[0]["d"])
        pred_d = [pp[0] for pp in pred_pairs]
        curr_d = [curr[i].waypoints[0]["d"] for i in cidx_sorted]
        asig = _alinear(pred_d, curr_d, dt)
        for k, i in enumerate(cidx_sorted):
            pi = asig[k]
            if pi is not None:
                ids[i] = pred_pairs[pi][1]

    for i in range(len(curr)):
        if ids[i] is None:
            ids[i] = next_id
            next_id += 1
    return ids, next_id

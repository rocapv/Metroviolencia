"""
Estimador de posiciones de trenes (v4).

Cambios sobre v3:
  * Orden y posición sobre el POLYLINE DE VÍA real de cada línea comercial
    (net.comerciales), no sobre segmentos rectos entre estaciones. Los trenes
    circulan por la vía.
  * Se emiten datos para animar en el cliente: distancia de la próxima parada a
    lo largo de la vía (d_next), velocidad (m/s), sentido y t_tramo. El frontend
    recalcula la posición viva con la cuenta atrás del ETA.

Salida por tren:
  {linea, destino, prox_estacion, anterior_estacion, eta_seg, color,
   lat, lng,                      # posición inicial (snapshot)
   d_next, speed, sentido, t_seg} # para animar sobre comerciales[linea]
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from statistics import median

from .network import Network

VEL_FALLBACK = 11.0
SLACK = 1.25
SEG_MIN, SEG_MAX = 20, 420


@dataclass
class Tren:
    linea: int
    destino: str
    lat: float
    lng: float
    prox_estacion_id: int
    prox_estacion: str
    anterior_estacion: str
    eta_seg: int
    color: str
    d_next: float
    speed: float
    sentido: int
    t_seg: float

    def to_dict(self) -> dict:
        return asdict(self)


def _tramos_cerrados(net: Network) -> set[frozenset]:
    cerrados: set[frozenset] = set()
    for c in net.cierres():
        tramo = c.get("tramo", [])
        for a, b in zip(tramo, tramo[1:]):
            cerrados.add(frozenset((a.strip().lower(), b.strip().lower())))
    return cerrados


def estimar(net: Network, llegadas: list[tuple[int, int, str, int]]) -> list[Tren]:
    cerrados = _tramos_cerrados(net)

    # índice de estación dentro del orden de cada línea comercial + nombre->id
    idx = {}
    nom2id = {}
    for lid, lc in net.comerciales.items():
        idx[lid] = {s: i for i, (_d, s) in enumerate(lc.orden)}
        nom2id[lid] = {net.estaciones[s].nombre.strip().lower(): s for _d, s in lc.orden}

    # ---- tiempo de tramo (s) desde los ETAs, por pareja de estaciones ----
    por_grupo: dict[tuple[int, str], list[tuple[float, int, int]]] = defaultdict(list)
    for est_id, linea, destino, eta in llegadas:
        lc = net.comerciales.get(linea)
        if lc and est_id in idx[linea]:
            d = lc.station_dist(est_id)
            por_grupo[(linea, destino)].append((d, est_id, eta))

    muestras: dict[tuple[int, frozenset], list[int]] = defaultdict(list)
    for (linea, _dest), items in por_grupo.items():
        items.sort()
        for (d0, s0, e0), (d1, s1, e1) in zip(items, items[1:]):
            if idx[linea].get(s1, -9) - idx[linea].get(s0, 99) != 1:
                continue
            dt = abs(e1 - e0)
            if SEG_MIN <= dt <= SEG_MAX:
                muestras[(linea, frozenset((s0, s1)))].append(dt)

    def t_tramo(linea, a, b, dist_m) -> float:
        m = muestras.get((linea, frozenset((a, b))))
        if m:
            return float(median(m))
        return dist_m / VEL_FALLBACK if dist_m else 60.0

    # ---- tren inminente por (estacion, linea, destino) ----
    minimos: dict[tuple[int, int, str], int] = {}
    for est_id, linea, destino, eta in llegadas:
        k = (est_id, linea, destino)
        if k not in minimos or eta < minimos[k]:
            minimos[k] = eta

    trenes: list[Tren] = []
    for (est_id, linea, destino), eta in minimos.items():
        lc = net.comerciales.get(linea)
        if not lc or est_id not in idx[linea]:
            continue
        orden_ids = [s for _d, s in lc.orden]
        i = idx[linea][est_id]
        d_next = lc.station_dist(est_id)

        # sentido: hacia el destino (por distancia sobre la vía)
        dest_sid = nom2id[linea].get(destino.strip().lower())
        if dest_sid is None:
            # coincidencia parcial
            for nom, sid in nom2id[linea].items():
                if destino.strip().lower() in nom or nom in destino.strip().lower():
                    dest_sid = sid
                    break
        if dest_sid is not None and dest_sid != est_id:
            sentido = 1 if lc.station_dist(dest_sid) > d_next else -1
        elif i == len(orden_ids) - 1:
            sentido = -1
        else:
            sentido = 1

        prev_i = i - sentido
        if prev_i < 0 or prev_i >= len(orden_ids):
            continue
        prev_id = orden_ids[prev_i]
        ea, eb = net.estaciones[prev_id], net.estaciones[est_id]
        if frozenset((ea.nombre.lower(), eb.nombre.lower())) in cerrados:
            continue

        d_prev = lc.station_dist(prev_id)
        dist_m = abs(d_next - d_prev)
        T = t_tramo(linea, prev_id, est_id, dist_m)
        if T <= 0 or eta > T * SLACK:
            continue
        speed = dist_m / T if T else VEL_FALLBACK
        d_live = d_next - sentido * speed * eta
        lat, lng = lc.dist_to_latlng(d_live)
        trenes.append(
            Tren(
                linea=linea,
                destino=destino,
                lat=lat,
                lng=lng,
                prox_estacion_id=est_id,
                prox_estacion=eb.nombre,
                anterior_estacion=ea.nombre,
                eta_seg=eta,
                color=lc.color,
                d_next=round(d_next, 1),
                speed=round(speed, 2),
                sentido=sentido,
                t_seg=round(T, 1),
            )
        )
    return trenes

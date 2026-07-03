"""
Estimador de posiciones de trenes (v3).

Entrada: llegadas de un snapshot -> [(estacion_id, linea, destino, eta_seg)].
Salida:  lista de trenes {linea, destino, lat, lng, prox_estacion, eta_seg, ...}.

Modelo "tren inminente por tramo" (ver v2) con estas mejoras:

  * Tiempo de recorrido POR TRAMO estimado de los propios datos: para cada línea
    y sentido, la diferencia de ETA entre estaciones consecutivas del recorrido es
    el tiempo real de ese tramo (incluye parada). Se agrega la mediana por tramo y
    se usa en vez de una velocidad global -> posiciones mucho más fieles.
  * Color por LÍNEA COMERCIAL (no por segmento de vía).
  * Se omiten los tramos en obras/cerrados (data/service.json): no se dibujan
    trenes fantasma en ellos.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from statistics import median

from .network import Network

VEL_FALLBACK = 11.0     # m/s (~40 km/h) si un tramo no tiene muestras
SLACK = 1.25            # margen sobre T_tramo antes de descartar el tren
SEG_MIN, SEG_MAX = 20, 420   # rango plausible de tiempo de tramo (s) para muestrear


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
    frac: float
    color: str

    def to_dict(self) -> dict:
        return asdict(self)


def _d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _chains(net: Network) -> dict[int, list[int]]:
    por_linea: dict[int, list[int]] = defaultdict(list)
    for e in net.estaciones.values():
        for lid in e.lineas:
            por_linea[lid].append(e.id)
    chains: dict[int, list[int]] = {}
    for lid, sids in por_linea.items():
        if len(sids) < 2:
            chains[lid] = sids
            continue
        pts = {s: net._xy_of(s) for s in sids}
        ids = list(pts)
        term, dmax = ids[0], -1.0
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                dd = _d(pts[a], pts[b])
                if dd > dmax:
                    dmax, term = dd, a
        rest = set(pts)
        chain = [term]
        rest.discard(term)
        cur = term
        while rest:
            nxt = min(rest, key=lambda s: _d(pts[cur], pts[s]))
            chain.append(nxt)
            rest.discard(nxt)
            cur = nxt
        chains[lid] = chain
    return chains


def _match_destino(net: Network, chain: list[int], destino: str) -> int | None:
    d = destino.strip().lower()
    for i, s in enumerate(chain):
        if net.estaciones[s].nombre.strip().lower() == d:
            return i
    for i, s in enumerate(chain):
        n = net.estaciones[s].nombre.strip().lower()
        if d in n or n in d:
            return i
    return None


def _tramos_cerrados(net: Network) -> set[frozenset]:
    """Pares de nombres de estación consecutivos que están cerrados."""
    cerrados: set[frozenset] = set()
    for c in net.cierres():
        tramo = c.get("tramo", [])
        for a, b in zip(tramo, tramo[1:]):
            cerrados.add(frozenset((a.strip().lower(), b.strip().lower())))
    return cerrados


def estimar(net: Network, llegadas: list[tuple[int, int, str, int]]) -> list[Tren]:
    chains = _chains(net)
    idx_en_chain = {
        lid: {s: i for i, s in enumerate(chain)} for lid, chain in chains.items()
    }
    cerrados = _tramos_cerrados(net)

    # ---- 1) muestrear tiempo de tramo (s) a partir de los ETAs ----
    # por (linea, destino): estaciones ordenadas por índice de chain, Δeta entre
    # consecutivas del recorrido -> tiempo de ese tramo.
    por_grupo: dict[tuple[int, str], list[tuple[int, int, int]]] = defaultdict(list)
    for est_id, linea, destino, eta in llegadas:
        if linea in idx_en_chain and est_id in idx_en_chain[linea]:
            por_grupo[(linea, destino)].append((idx_en_chain[linea][est_id], est_id, eta))

    muestras_tramo: dict[tuple[int, frozenset], list[int]] = defaultdict(list)
    for (linea, _destino), items in por_grupo.items():
        items.sort()  # por índice de recorrido
        for (i0, s0, e0), (i1, s1, e1) in zip(items, items[1:]):
            if abs(i1 - i0) != 1:
                continue  # no son estaciones adyacentes en el recorrido
            dt = abs(e1 - e0)
            if SEG_MIN <= dt <= SEG_MAX:
                muestras_tramo[(linea, frozenset((s0, s1)))].append(dt)

    def t_tramo(linea, a_id, b_id) -> float:
        key = (linea, frozenset((a_id, b_id)))
        m = muestras_tramo.get(key)
        if m:
            return float(median(m))
        return _d(net._xy_of(a_id), net._xy_of(b_id)) / VEL_FALLBACK

    # ---- 2) tren inminente por (estacion, linea, destino) ----
    minimos: dict[tuple[int, int, str], int] = {}
    for est_id, linea, destino, eta in llegadas:
        k = (est_id, linea, destino)
        if k not in minimos or eta < minimos[k]:
            minimos[k] = eta

    trenes: list[Tren] = []
    for (est_id, linea, destino), eta in minimos.items():
        chain = chains.get(linea)
        if not chain or est_id not in idx_en_chain[linea]:
            continue
        i = idx_en_chain[linea][est_id]
        dest_idx = _match_destino(net, chain, destino)
        if dest_idx is not None and dest_idx == i:
            paso = -1 if i == len(chain) - 1 else 1
            prev_i = i - paso
        elif dest_idx is not None:
            paso = 1 if dest_idx > i else -1
            prev_i = i - paso
        else:
            prev_i = i - 1 if i > 0 else i + 1
        if prev_i < 0 or prev_i >= len(chain):
            continue

        prev_id = chain[prev_i]
        ea, eb = net.estaciones[prev_id], net.estaciones[est_id]
        # tramo cerrado -> no dibujar
        if frozenset((ea.nombre.lower(), eb.nombre.lower())) in cerrados:
            continue

        T = t_tramo(linea, prev_id, est_id)
        if T <= 0 or eta > T * SLACK:
            continue
        frac = max(0.0, min(1.0, 1.0 - eta / T))
        lat = ea.lat + frac * (eb.lat - ea.lat)
        lng = ea.lng + frac * (eb.lng - ea.lng)
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
                frac=round(frac, 3),
                color=net.colores_comercial.get(str(linea), "888888"),
            )
        )
    return trenes

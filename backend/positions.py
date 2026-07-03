"""
Estimador de posiciones de trenes (v2).

Entrada: llegadas por estación de un snapshot -> [(estacion_id, linea, destino, eta_seg)].
Salida:  lista de trenes {linea, destino, lat, lng, prox_estacion, eta_seg}.

Modelo v2 — "tren inminente por tramo"
--------------------------------------
Para cada estación S, línea L y sentido (destino D) tomamos la llegada de MENOR
ETA: es el tren que está a punto de llegar a S. Ese tren se encuentra en el tramo
[estación_anterior -> S] del recorrido. Lo situamos según su ETA:

    fracción_recorrida = 1 - eta / T_tramo        (0 = acaba de salir de la
                                                    anterior, 1 = entrando en S)

donde T_tramo = distancia(anterior, S) / velocidad. Si eta > T_tramo el tren aún
no ha entrado en este tramo (será el tren inminente de la estación anterior), así
que no se dibuja aquí: eso garantiza ~un punto por tramo ocupado y evita el doble
conteo del mismo tren físico.

El sentido se deduce del destino D: hacia qué extremo de la línea circula.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict

from .network import Network

VEL = 11.0          # m/s (~40 km/h) velocidad media asumida
SLACK = 1.20        # margen sobre T_tramo antes de descartar el tren


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
        # terminal = un extremo del par más separado
        ids = list(pts)
        term, dmax = ids[0], -1.0
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                dd = _d(pts[a], pts[b])
                if dd > dmax:
                    dmax, term = dd, a
        # encadenado por vecino más próximo desde el terminal
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
    """Índice en la chain de la estación destino (por nombre)."""
    d = destino.strip().lower()
    for i, s in enumerate(chain):
        if net.estaciones[s].nombre.strip().lower() == d:
            return i
    # coincidencia parcial
    for i, s in enumerate(chain):
        n = net.estaciones[s].nombre.strip().lower()
        if d in n or n in d:
            return i
    return None


def estimar(net: Network, llegadas: list[tuple[int, int, str, int]]) -> list[Tren]:
    chains = _chains(net)
    idx_en_chain = {
        lid: {s: i for i, s in enumerate(chain)} for lid, chain in chains.items()
    }

    # tren inminente por (estacion, linea, destino) = menor ETA
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
        # sentido: hacia el destino. anterior = estación previa en ese sentido.
        if dest_idx is not None and dest_idx == i:
            # S es el propio destino (terminal): el tren llega desde el lado interior
            paso = -1 if i == len(chain) - 1 else 1
            prev_i = i - paso
        elif dest_idx is not None:
            paso = 1 if dest_idx > i else -1
            prev_i = i - paso
        else:
            # destino desconocido: asume que viene del vecino de mayor índice
            prev_i = i - 1 if i > 0 else i + 1

        if prev_i < 0 or prev_i >= len(chain):
            continue
        prev_id = chain[prev_i]
        seg = _d(net._xy_of(prev_id), net._xy_of(est_id))
        if seg <= 0:
            continue
        t_tramo = seg / VEL
        if eta > t_tramo * SLACK:
            continue  # aún no está en este tramo
        frac = max(0.0, min(1.0, 1.0 - eta / t_tramo))
        ea, eb = net.estaciones[prev_id], net.estaciones[est_id]
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
                color=net.lineas[linea].color if linea in net.lineas else "888888",
            )
        )
    return trenes

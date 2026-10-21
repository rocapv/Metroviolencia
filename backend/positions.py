"""
Estimador de posiciones de trenes (v5) — trenes FÍSICOS con trayectoria.

Problema de v4: modelaba "próxima llegada por estación" (un punto por estación).
Al llegar a su parada el punto se congelaba, y al siguiente pull la misma clave
se reasignaba al tren de detrás -> el punto saltaba atrás (bucle infinito).

v5 reconstruye cada TREN FÍSICO y su trayectoria hacia delante:

  Para una (línea, destino), las estaciones en orden de recorrido hacia el
  destino tienen ETAs crecientes MIENTRAS sean del mismo tren (está por delante
  de todas ellas, es su próxima llegada). Donde el ETA CAE bruscamente empieza
  otro tren por delante. Así, recorriendo las estaciones en orden, cada tramo de
  ETA creciente = un tren, con sus próximas paradas y tiempos.

Cada tren emite una lista de "waypoints" [{d, eta}] (distancia sobre la vía y
segundos hasta esa parada). El frontend interpola la posición viva con el reloj:
el punto avanza por la vía cruzando estaciones de paso y solo se detiene el poco
que marcan los propios tiempos (y en cabeceras, donde el tren espera a salir).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, asdict
from statistics import median

from .network import Network

VEL_FALLBACK = 11.0
SEG_MIN, SEG_MAX = 20, 420
CAIDA_MIN = 45  # s: caída de ETA que marca el cambio a otro tren por delante
DEDUP_TOL = 130  # m: dos trenes del MISMO (línea,destino) más cerca que esto son
                 # el mismo tren físico contado dos veces (artefacto de cabecera).


def _mejor(a: "Tren", b: "Tren", por_parada: bool = False) -> bool:
    """True si `a` representa mejor el tren físico que `b` (para quedarnos con uno
    al colapsar un duplicado). Preferimos el que NO espera en cabecera (tren real
    en marcha), luego el de trayectoria más rica, luego la vía principal, luego
    el de llegada más próxima. Si el duplicado se detectó por "misma próxima
    parada y mismo ETA" (ramal vs tronco), manda primero la trayectoria más rica:
    la variante que lo vio en su cabecera del ramal sabe más que el tronco, que
    solo lo deduce de la parada de unión."""
    if por_parada and len(a.waypoints) != len(b.waypoints):
        return len(a.waypoints) > len(b.waypoints)
    if a.origen != b.origen:
        return b.origen            # nos quedamos con el que no es 'origen'
    if len(a.waypoints) != len(b.waypoints):
        return len(a.waypoints) > len(b.waypoints)
    if a.via != b.via:
        return a.via == a.linea    # en el tronco, la vía principal (d estables)
    return a.eta_seg < b.eta_seg


def _metros(a: "Tren", b: "Tren") -> float:
    return math.hypot((a.lat - b.lat) * 111000, (a.lng - b.lng) * 85000)


DEDUP_ETA = 60   # s: misma próxima parada y ETA a menos de esto = mismo tren


def _paradas(t: "Tren") -> frozenset:
    return frozenset((w["nom"], w["eta"]) for w in t.waypoints if w.get("nom"))


def _dedup(trenes: list["Tren"]) -> list["Tren"]:
    """Colapsa duplicados de cabecera/terminal: la fuente lista, en la misma
    terminal, el tren que ACABA de llegar y su próxima SALIDA; el estimador crea
    dos `Tren` en la misma (línea,destino) y misma posición. Dos trenes reales del
    mismo destino van siempre a un headway (cientos/miles de m), así que dos a
    < DEDUP_TOL son el mismo: nos quedamos con el mejor (ver `_mejor`)."""
    por: dict[tuple[int, str], list[Tren]] = defaultdict(list)
    for t in trenes:
        por[(t.linea, t.destino)].append(t)
    out: list[Tren] = []
    for _key, lst in por.items():
        lst.sort(key=lambda t: (t.via != t.linea, t.waypoints[0]["d"]))
        kept: list[Tren] = []
        for t in lst:
            dup_i, por_parada = None, False
            for i, k in enumerate(kept):
                if _metros(k, t) < DEDUP_TOL:
                    dup_i = i
                    break
                # ramal vs tronco: el mismo tren visto desde dos vías. Las paradas
                # (nombre, ETA) de una trayectoria están contenidas en las de la
                # otra (la vía que lo vio en su cabecera del ramal sabe más) o
                # comparten la próxima parada con el mismo ETA.
                if k.via != t.via:
                    pk, pt = _paradas(k), _paradas(t)
                    if (pk and pt and (pk <= pt or pt <= pk)) or (
                            k.prox_estacion == t.prox_estacion
                            and abs(k.eta_seg - t.eta_seg) <= DEDUP_ETA):
                        dup_i, por_parada = i, True
                        break
            if dup_i is None:
                kept.append(t)
            elif _mejor(t, kept[dup_i], por_parada):
                kept[dup_i] = t
        out.extend(kept)
    return out


@dataclass
class Tren:
    linea: int
    destino: str
    color: str
    lat: float
    lng: float
    prox_estacion: str
    eta_seg: int          # a la próxima parada
    waypoints: list        # [{"d":m, "eta":s}, ...] hacia delante
    origen: bool           # True si está esperando en cabecera (parado)
    via: int = 0           # línea comercial que da la geometría (= linea, o una variante de ramal)

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

    idx = {}       # lid -> {station_id: pos en orden de vía}
    dist = {}      # lid -> {station_id: distancia sobre la vía}
    nom2id = {}    # lid -> {nombre: id}
    orden_ids = {}  # lid -> [station_id...] por distancia ascendente
    for lid, lc in net.comerciales.items():
        orden_ids[lid] = [s for _d, s in lc.orden]
        idx[lid] = {s: i for i, s in enumerate(orden_ids[lid])}
        dist[lid] = {s: d for d, s in lc.orden}
        nom2id[lid] = {net.estaciones[s].nombre.strip().lower(): s for _d, s in lc.orden}

    # ETAs por (linea, destino) y por estación
    grupos: dict[tuple[int, str], dict[int, int]] = defaultdict(dict)
    for est_id, linea, destino, eta in llegadas:
        # la estación puede ser de la línea principal o de una variante de ramal
        if linea in idx and any(est_id in idx[v] for v in net.vias_de(linea)):
            prev = grupos[(linea, destino)].get(est_id)
            if prev is None or eta < prev:
                grupos[(linea, destino)][est_id] = eta

    retorno = set(net.estaciones_retorno())
    con_dato: dict[int, set[int]] = defaultdict(set)  # linea -> estaciones con llegadas
    for (linea, _dest), etas in grupos.items():
        con_dato[linea].update(etas)

    # tiempo de tramo (s) por pareja de estaciones, de los propios ETAs
    muestras: dict[tuple[int, frozenset], list[int]] = defaultdict(list)
    for (linea, _dest), etas in grupos.items():
        for via in net.vias_de(linea):        # la adyacencia es la de cada vía (ramales)
            etas_v = {s: e for s, e in etas.items() if s in idx[via]}
            seq = sorted(etas_v.items(), key=lambda kv: idx[via][kv[0]])
            for (s0, e0), (s1, e1) in zip(seq, seq[1:]):
                if abs(idx[via][s1] - idx[via][s0]) == 1 and SEG_MIN <= abs(e1 - e0) <= SEG_MAX:
                    muestras[(linea, frozenset((s0, s1)))].append(abs(e1 - e0))

    def segtime(linea, via, a, b):
        m = muestras.get((linea, frozenset((a, b))))
        if m:
            return float(median(m))
        d = abs(dist[via][a] - dist[via][b])   # la vía (ramal) tiene la geometría
        return d / VEL_FALLBACK if d else 60.0

    trenes: list[Tren] = []
    for (linea, destino), etas_todas in grupos.items():
      # una pasada por VÍA: la línea principal y cada variante de ramal (L4: 4, 41, 42).
      # Cada vía solo ve sus estaciones; en el tronco todas ven lo mismo y _dedup
      # colapsa los duplicados; en un ramal solo su variante tiene las estaciones.
      for via in net.vias_de(linea):
        if via not in idx:
            continue
        lc = net.comerciales[via]
        etas = {s: e for s, e in etas_todas.items() if s in idx[via]}
        if not etas:
            continue
        # sentido: hacia el destino
        dest_sid = nom2id[via].get(destino.strip().lower())
        if dest_sid is None:
            for nom, sid in nom2id[via].items():
                if destino.strip().lower() in nom or nom in destino.strip().lower():
                    dest_sid = sid
                    break
        if dest_sid is None and any(destino.strip().lower() in nom2id[v]
                                    for v in net.vias_de(linea) if v != via):
            continue      # el destino es de otro ramal: lo lleva esa variante
        # estaciones con dato, en orden de RECORRIDO hacia el destino
        seq = sorted(etas.items(), key=lambda kv: idx[via][kv[0]])
        if dest_sid is not None and dist[via].get(dest_sid, 0) < (
            dist[via][seq[0][0]] if seq else 0
        ):
            seq = list(reversed(seq))
            sentido = -1
        else:
            sentido = 1
        if not seq:
            continue

        # partir en trenes: un tramo de ETA creciente = un tren; caída = nuevo tren
        runs: list[list[tuple[int, int]]] = [[]]
        prev_eta = None
        for s, e in seq:
            if prev_eta is not None and e < prev_eta - CAIDA_MIN:
                runs.append([])
            runs[-1].append((s, e))
            prev_eta = e

        for run in runs:
            if not run:
                continue
            s0, e0 = run[0]  # próxima parada de este tren
            # estación anterior en el recorrido (de la que viene)
            i0 = idx[via][s0]
            prev_i = i0 - sentido
            waypoints = []
            origen = False
            # La estación anterior anuncia esta línea pero no este destino: el
            # servicio NACE en s0 (p.ej. L4 Empalme -> Ll. Llarga; Palau no lo
            # anuncia). Está en cabecera, no viene de la anterior.
            # Solo en estaciones de retorno: un destino poco frecuente puede no
            # salir en el panel (corto) de la anterior sin que el tren nazca aquí.
            atras = (orden_ids[via][prev_i::-1] if sentido > 0
                     else orden_ids[via][prev_i:]) if 0 <= prev_i < len(orden_ids[via]) else []
            nace_aqui = (net.estaciones[s0].nombre in retorno
                         and bool(atras) and atras[0] in con_dato[linea]
                         and not any(s in etas for s in atras))
            if nace_aqui:
                origen = True
                waypoints.append({"d": round(dist[via][s0], 1), "eta": 0})
            elif 0 <= prev_i < len(orden_ids[via]):
                prev_id = orden_ids[via][prev_i]
                ea, eb = net.estaciones[prev_id], net.estaciones[s0]
                if frozenset((ea.nombre.lower(), eb.nombre.lower())) in cerrados:
                    continue
                st = segtime(linea, via, prev_id, s0)
                seg_d = abs(dist[via][s0] - dist[via][prev_id])
                speed = seg_d / st if st else VEL_FALLBACK
                d_now = dist[via][s0] - sentido * speed * e0
                # no retroceder más allá de la estación anterior
                lo, hi = sorted((dist[via][prev_id], dist[via][s0]))
                d_now = max(lo, min(hi, d_now))
                waypoints.append({"d": round(d_now, 1), "eta": 0})
            else:
                # sin estación anterior: en cabecera esperando salida
                origen = True
                waypoints.append({"d": round(dist[via][s0], 1), "eta": 0})

            for s, e in run:
                waypoints.append({"d": round(dist[via][s], 1), "eta": int(e),
                                  "nom": net.estaciones[s].nombre})

            lat, lng = lc.dist_to_latlng(waypoints[0]["d"])
            trenes.append(Tren(
                linea=linea, destino=destino, color=lc.color,
                lat=lat, lng=lng,
                prox_estacion=net.estaciones[s0].nombre, eta_seg=int(e0),
                waypoints=waypoints, origen=origen, via=via,
            ))
    return _dedup(trenes)

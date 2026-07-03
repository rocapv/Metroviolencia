"""
Colapsa los raíles paralelos (doble vía) de los trazados de tranvía a una línea
central única, conservando ramales.

Método: por cada línea, construye un grafo con los puntos de las vías OSM (nodos
deduplicados a ~4 m, aristas entre puntos consecutivos + puentes entre extremos
próximos). Luego crea un ÁRBOL que conecta todas las estaciones de la línea por
caminos mínimos (Dijkstra), creciendo desde un terminal. Como es un árbol, los
raíles paralelos colapsan a uno solo (el camino mínimo elige un raíl) y los
ramales se conservan (cada estación se engancha al árbol por su camino más corto).

Entrada:  data/tram_tracks.json  (crudo OSM; se respalda a tram_tracks.raw.json)
Salida:   data/tram_tracks.json  (colapsado: {ref: [[[lat,lng],...], ...]})
"""
import heapq
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TT = ROOT / "data" / "tram_tracks.json"
RAW = ROOT / "data" / "tram_tracks.raw.json"

NODE_GRID = 4.0      # m: dedup de nodos
BRIDGE = 22.0        # m: unir extremos de vías próximos (y cruces entre raíles)


def _m(a, b):
    return math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 85000)


def _key(p):
    # rejilla ~NODE_GRID metros
    return (round(p[0] * 111000 / NODE_GRID), round(p[1] * 85000 / NODE_GRID))


def build_graph(ways):
    nodes = {}          # key -> (lat,lng) representativo
    adj = {}            # key -> {key: dist}
    def node(p):
        k = _key(p)
        if k not in nodes:
            nodes[k] = p
            adj[k] = {}
        return k
    def edge(k1, k2):
        if k1 == k2:
            return
        d = _m(nodes[k1], nodes[k2])
        if adj[k1].get(k2, 1e9) > d:
            adj[k1][k2] = d
            adj[k2][k1] = d
    endpoints = []
    for w in ways:
        prev = None
        for p in w:
            k = node(p)
            if prev is not None:
                edge(prev, k)
            prev = k
        if w:
            endpoints.append(node(w[0]))
            endpoints.append(node(w[-1]))
    # puentes entre extremos próximos (conecta vías y cruces entre raíles)
    for i in range(len(endpoints)):
        for j in range(i + 1, len(endpoints)):
            a, b = endpoints[i], endpoints[j]
            if a != b and _m(nodes[a], nodes[b]) < BRIDGE:
                edge(a, b)
    _connect_components(nodes, adj, edge)
    return nodes, adj


def _components(adj):
    seen, comps = set(), []
    for start in adj:
        if start in seen:
            continue
        stack, comp = [start], []
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            comp.append(u)
            stack.extend(adj[u])
        comps.append(comp)
    return comps


def _connect_components(nodes, adj, edge, max_gap=250.0):
    """Une componentes desconectados por su par de nodos más próximo (evita que
    tramos de vía sueltos —p. ej. la costa— dejen estaciones huérfanas)."""
    comps = _components(adj)
    while len(comps) > 1:
        # el componente más pequeño se une al resto por el par de nodos más próximo
        comps.sort(key=len)
        small = comps[0]
        others = [n for c in comps[1:] for n in c]
        best = None
        for a in small:
            for b in others:
                d = _m(nodes[a], nodes[b])
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best is None or best[0] > max_gap:
            break
        edge(best[1], best[2])
        comps = _components(adj)


def dijkstra(adj, src, targets):
    """Camino mínimo de src a cualquiera de targets. Devuelve (dest, prev)."""
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    tset = set(targets)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18):
            continue
        if u in tset:
            return u, prev
        for v, w in adj[u].items():
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return None, prev


def path_between(adj, a, b):
    dest, prev = dijkstra(adj, a, [b])
    if dest is None:
        return None
    path = [dest]
    while path[-1] != a:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def collapse_line(ways, station_coords):
    nodes, adj = build_graph(ways)
    if not nodes:
        return ways
    # estaciones -> nodo más próximo
    snodes = []
    for s in station_coords:
        best, bd = None, 1e18
        for k, p in nodes.items():
            d = _m(s, p)
            if d < bd:
                bd, best = d, k
        if best is not None:
            snodes.append(best)
    snodes = list(dict.fromkeys(snodes))
    if len(snodes) < 2:
        return ways
    # árbol: empezar por un terminal (el par más separado), enganchar el resto
    tree_nodes = {snodes[0]}
    segments = []
    remaining = snodes[1:]
    # ordenar por cercanía incremental
    while remaining:
        # estación más próxima a algún nodo del árbol (por grafo, aprox euclídea)
        remaining.sort(key=lambda s: min(_m(nodes[s], nodes[t]) for t in tree_nodes))
        s = remaining.pop(0)
        dest, prev = dijkstra(adj, s, list(tree_nodes))
        if dest is None:
            continue
        path = [dest]
        while path[-1] != s:
            path.append(prev[path[-1]])
        # path va de dest(en árbol) a s
        seg = [nodes[k] for k in path]
        segments.append(seg)
        for k in path:
            tree_nodes.add(k)
    return segments if segments else ways


def main():
    if not RAW.exists():
        RAW.write_text(TT.read_text(encoding="utf-8"), encoding="utf-8")
    tt = json.loads(RAW.read_text(encoding="utf-8"))

    import sys
    sys.path.insert(0, str(ROOT))
    from backend.network import load_network
    net = load_network()

    out = {}
    for ref, ways in tt.items():
        lid = int(ref)
        lc = net.comerciales.get(lid)
        st = [(net.estaciones[s].lat, net.estaciones[s].lng) for _d, s in (lc.orden if lc else [])]
        collapsed = collapse_line(ways, st)
        out[ref] = collapsed
        npts = sum(len(w) for w in collapsed)
        print(f"L{ref}: {len(ways)} vías -> {len(collapsed)} tramos, {npts} pts")
    TT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print("escrito", TT)


if __name__ == "__main__":
    main()

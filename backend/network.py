"""
Geometría de la red: carga data/network.json y construye, por cada línea,
la polyline y la secuencia ordenada de estaciones proyectadas sobre ella,
con su distancia acumulada. Permite convertir "distancia a lo largo de la
línea" <-> coordenada (lat,lng), que es la base para situar trenes.

Distancias en metros (aprox. equirectangular, suficiente a escala urbana).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NETWORK_JSON = ROOT / "data" / "network.json"
SERVICE_JSON = ROOT / "data" / "service.json"
# Trazado real de las líneas de tranvía (L4/L6/L8/L10) desde OpenStreetMap
# (railway=tram). Metrovalencia no da polylines limpias de tranvía. Generado con
# scripts/fetch_tram_tracks.py.
TRAM_TRACKS_JSON = ROOT / "data" / "tram_tracks.json"

_R = 6371000.0  # radio terrestre m
_LAT0 = math.radians(39.47)  # latitud de referencia (Valencia) para el coseno


def _xy(lat: float, lng: float) -> tuple[float, float]:
    """Proyección equirectangular local a metros."""
    x = math.radians(lng) * _R * math.cos(_LAT0)
    y = math.radians(lat) * _R
    return x, y


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _project_point_on_segment(p, a, b):
    """Proyecta p sobre el segmento ab. Devuelve (t en [0,1], punto, dist_perp)."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return 0.0, a, _dist(p, a)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    proj = (ax + t * dx, ay + t * dy)
    return t, proj, _dist(p, proj)


@dataclass
class Estacion:
    id: int
    nombre: str
    lat: float
    lng: float
    lineas: list[int]


@dataclass
class Linea:
    id: int
    color: str
    # polyline en metros (x,y) y su versión latlng
    pts_xy: list[tuple[float, float]]
    latlng: list[tuple[float, float]]
    cum: list[float]  # distancia acumulada por vértice
    # estaciones ordenadas por distancia sobre la línea: [(dist, station_id)]
    orden: list[tuple[float, int]] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def dist_to_latlng(self, d: float) -> tuple[float, float]:
        """Convierte distancia acumulada -> (lat,lng) caminando la polyline."""
        d = max(0.0, min(self.length, d))
        # búsqueda del segmento
        lo, hi = 0, len(self.cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        i = max(1, lo)
        seg_len = self.cum[i] - self.cum[i - 1]
        t = 0.0 if seg_len == 0 else (d - self.cum[i - 1]) / seg_len
        la0, ln0 = self.latlng[i - 1]
        la1, ln1 = self.latlng[i]
        return (la0 + t * (la1 - la0), ln0 + t * (ln1 - ln0))

    def station_dist(self, sid: int) -> float | None:
        for d, s in self.orden:
            if s == sid:
                return d
        return None


@dataclass
class LineaComercial:
    """Línea comercial (1..10) mapeada sobre su polyline de vía real."""
    id: int
    color: str
    seg: str                       # clave del polyline de vía elegido
    latlng: list[tuple[float, float]]
    cum: list[float]
    orden: list[tuple[float, int]]  # (dist_along, station_id) ordenado
    estaciones: set[int]

    @property
    def length(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def station_dist(self, sid: int) -> float | None:
        for d, s in self.orden:
            if s == sid:
                return d
        return None

    def dist_to_latlng(self, d: float) -> tuple[float, float]:
        d = max(0.0, min(self.length, d))
        lo, hi = 0, len(self.cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        i = max(1, lo)
        seg_len = self.cum[i] - self.cum[i - 1]
        t = 0.0 if seg_len == 0 else (d - self.cum[i - 1]) / seg_len
        la0, ln0 = self.latlng[i - 1]
        la1, ln1 = self.latlng[i]
        return (la0 + t * (la1 - la0), ln0 + t * (ln1 - ln0))


class Network:
    def __init__(self, data: dict):
        self.colores: dict[str, str] = data["colores"]
        # color por línea COMERCIAL (1..10); fallback a coloresLineas
        self.colores_comercial: dict[str, str] = data.get(
            "colores_comercial", {}
        ) or data["colores"]
        self.estaciones: dict[int, Estacion] = {}
        for e in data["estaciones"]:
            if e["lat"] is None:
                continue
            self.estaciones[e["id"]] = Estacion(
                e["id"], e["nombre"], e["lat"], e["lng"], e["lineas"]
            )
        self.lineas: dict[int, Linea] = {}
        for lid_str, pts in data["lineas"].items():
            lid = int(lid_str)
            latlng = [(p[0], p[1]) for p in pts]
            pts_xy = [_xy(la, ln) for la, ln in latlng]
            cum = [0.0]
            for i in range(1, len(pts_xy)):
                cum.append(cum[-1] + _dist(pts_xy[i - 1], pts_xy[i]))
            self.lineas[lid] = Linea(
                id=lid,
                color=self.colores.get(lid_str, "888888"),
                pts_xy=pts_xy,
                latlng=latlng,
                cum=cum,
            )
        self._build_orden()
        self._build_comerciales()

    def _build_orden(self):
        """Proyecta cada estación sobre las líneas a las que pertenece."""
        for est in self.estaciones.values():
            p = _xy(est.lat, est.lng)
            for lid in est.lineas:
                ln = self.lineas.get(lid)
                if not ln:
                    continue
                best = None  # (dist_perp, dist_along)
                for i in range(1, len(ln.pts_xy)):
                    t, _proj, dperp = _project_point_on_segment(
                        p, ln.pts_xy[i - 1], ln.pts_xy[i]
                    )
                    dalong = ln.cum[i - 1] + t * (ln.cum[i] - ln.cum[i - 1])
                    if best is None or dperp < best[0]:
                        best = (dperp, dalong)
                if best:
                    ln.orden.append((best[1], est.id))
        for ln in self.lineas.values():
            ln.orden.sort(key=lambda x: x[0])

    def _proj_en_linea(self, sid: int, ln: "Linea") -> tuple[float, float]:
        """(dist_along, dist_perp) de la estación sid sobre la polyline ln."""
        p = self._xy_of(sid)
        best = None
        for i in range(1, len(ln.pts_xy)):
            t, _pr, dperp = _project_point_on_segment(p, ln.pts_xy[i - 1], ln.pts_xy[i])
            dalong = ln.cum[i - 1] + t * (ln.cum[i] - ln.cum[i - 1])
            if best is None or dperp < best[1]:
                best = (dalong, dperp)
        return best if best else (0.0, 1e9)

    def _build_comerciales(self):
        """Mapea cada línea comercial a su polyline de vía (mejor cobertura) y
        ordena sus estaciones proyectándolas sobre ella."""
        por_linea: dict[int, list[int]] = {}
        for e in self.estaciones.values():
            for lid in e.lineas:
                por_linea.setdefault(lid, []).append(e.id)

        self.comerciales: dict[int, LineaComercial] = {}
        for lid, sids in por_linea.items():
            # elegir polyline con más estaciones a <80 m
            mejor, mejor_n = None, -1
            for pk, ln in self.lineas.items():
                if len(ln.pts_xy) < 2:
                    continue
                n = sum(1 for s in sids if self._proj_en_linea(s, ln)[1] < 80)
                if n > mejor_n:
                    mejor_n, mejor = n, pk
            if mejor is None:
                continue
            ln = self.lineas[mejor]
            orden = sorted(((self._proj_en_linea(s, ln)[0], s) for s in sids))
            self.comerciales[lid] = LineaComercial(
                id=lid,
                color=self.colores_comercial.get(str(lid), "888888"),
                seg=mejor,
                latlng=list(ln.latlng),
                cum=list(ln.cum),
                orden=orden,
                estaciones=set(sids),
            )

    def _xy_of(self, sid: int) -> tuple[float, float]:
        e = self.estaciones[sid]
        return _xy(e.lat, e.lng)

    def orden_nombres(self, lid: int) -> list[str]:
        ln = self.lineas[lid]
        return [self.estaciones[s].nombre for _, s in ln.orden]

    def tram_tracks(self) -> dict[str, list]:
        if not TRAM_TRACKS_JSON.exists():
            return {}
        try:
            return json.loads(TRAM_TRACKS_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def conectores(self) -> list[dict]:
        """Enlaces de vía que faltan en las polylines: entre estaciones vecinas
        de una misma línea comercial cuyo punto medio queda lejos de toda vía.
        Se omiten las líneas de tranvía (su trazado real viene de OSM)."""
        con_tram = set(int(k) for k in self.tram_tracks())
        # todos los segmentos de polyline en XY
        segs = []
        for ln in self.lineas.values():
            for i in range(1, len(ln.pts_xy)):
                segs.append((ln.pts_xy[i - 1], ln.pts_xy[i]))

        def lejos_de_via(mid_xy) -> bool:
            best = 1e9
            for a, b in segs:
                _t, _pr, dp = _project_point_on_segment(mid_xy, a, b)
                if dp < best:
                    best = dp
                    if best <= 120:
                        return False
            return best > 120

        por_linea: dict[int, list[int]] = {}
        for e in self.estaciones.values():
            for lid in e.lineas:
                por_linea.setdefault(lid, []).append(e.id)

        vistos: set[frozenset] = set()
        out: list[dict] = []
        for lid, sids in por_linea.items():
            if lid in con_tram:
                continue  # el trazado de tranvía lo aporta OSM
            color = self.colores_comercial.get(str(lid), "888888")
            for s in sids:
                p = self._xy_of(s)
                vecinos = sorted(
                    (x for x in sids if x != s),
                    key=lambda o: _dist(p, self._xy_of(o)),
                )[:1]
                for v in vecinos:
                    ea, eb = self.estaciones[s], self.estaciones[v]
                    d = _dist(p, self._xy_of(v))
                    if d > 1300:
                        continue
                    key = frozenset((s, v))
                    if key in vistos:
                        continue
                    mid = _xy((ea.lat + eb.lat) / 2, (ea.lng + eb.lng) / 2)
                    if lejos_de_via(mid):
                        vistos.add(key)
                        out.append({
                            "linea": lid, "color": color,
                            "path": [[ea.lat, ea.lng], [eb.lat, eb.lng]],
                            "a": ea.nombre, "b": eb.nombre,
                        })
        return out

    def _service(self) -> dict:
        if not SERVICE_JSON.exists():
            return {}
        try:
            return json.loads(SERVICE_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def cierres(self) -> list[dict]:
        return self._service().get("cierres", [])

    def subterraneos(self) -> list[dict]:
        return self._service().get("subterraneos", [])

    def estaciones_retorno(self) -> list[str]:
        """Cabeceras intermedias + finales de línea donde los trenes descansan
        e invierten el sentido."""
        retorno = set(self._service().get("estaciones_retorno", []))
        for lc in self.comerciales.values():
            if lc.orden:
                retorno.add(self.estaciones[lc.orden[0][1]].nombre)
                retorno.add(self.estaciones[lc.orden[-1][1]].nombre)
        return sorted(retorno)

    def to_geojson_meta(self) -> dict:
        """Datos estáticos para el frontend."""
        return {
            "colores": self.colores,
            "colores_comercial": self.colores_comercial,
            # tranvías: la fuente no da tiempo real -> siempre "sense info"
            "sin_tiempo_real": [4, 6, 8],
            "cierres": self.cierres(),
            "subterraneos": self.subterraneos(),
            "estaciones_retorno": self.estaciones_retorno(),
            "conectores": self.conectores(),
            "tram_tracks": self.tram_tracks(),
            # segmentos de vía crudos a dibujar = solo los de METRO (los del
            # tranvía se dibujan desde OSM; dibujar ambos duplicaba carriles)
            "metro_segs": sorted({
                lc.seg for lid, lc in self.comerciales.items()
                if str(lid) not in self.tram_tracks()
            }),
            "comerciales": {
                str(lid): {
                    "color": lc.color,
                    "path": [[la, lng] for la, lng in lc.latlng],
                    "cum": [round(c, 1) for c in lc.cum],
                    "length": lc.length,
                    "estaciones": [{"id": s, "d": round(d, 1)} for d, s in lc.orden],
                }
                for lid, lc in self.comerciales.items()
            },
            "lineas": {
                str(lid): {
                    "color": ln.color,
                    "path": [[la, lng] for la, lng in ln.latlng],
                }
                for lid, ln in self.lineas.items()
            },
            "estaciones": [
                {
                    "id": e.id,
                    "nombre": e.nombre,
                    "lat": e.lat,
                    "lng": e.lng,
                    "lineas": e.lineas,
                }
                for e in self.estaciones.values()
            ],
        }


@lru_cache(maxsize=1)
def load_network() -> Network:
    data = json.loads(NETWORK_JSON.read_text(encoding="utf-8"))
    return Network(data)


if __name__ == "__main__":
    net = load_network()
    print(f"{len(net.estaciones)} estaciones, {len(net.lineas)} líneas")
    for lid in sorted(net.lineas):
        ln = net.lineas[lid]
        nombres = net.orden_nombres(lid)
        print(f"L{lid}: {len(ln.orden)} est, {ln.length/1000:.1f} km  "
              f"{nombres[0]} ... {nombres[-1]}" if nombres else f"L{lid}: vacía")

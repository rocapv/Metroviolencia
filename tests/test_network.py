"""backend/network.py: carga de la red, líneas comerciales, cierres."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from backend import network
from backend.cabeceras import CABECERAS
from backend.network import Network, load_network

LINEAS_COMERCIALES = list(range(1, 11))
TRANVIAS = {4, 6, 8, 10}


def test_144_estaciones(net):
    assert len(net.estaciones) == 144
    for sid, e in net.estaciones.items():
        assert e.id == sid
        assert e.nombre
        assert -90 < e.lat < 90 and -180 < e.lng < 180
        assert e.lineas and all(l in LINEAS_COMERCIALES for l in e.lineas)


def test_load_network_cacheada(net):
    assert load_network() is net


def _principales(net):
    return sorted(k for k, lc in net.comerciales.items() if lc.variante_de is None)


def test_lineas_comerciales_1_a_10(net):
    assert _principales(net) == LINEAS_COMERCIALES
    for lid, lc in net.comerciales.items():
        assert lc.id == lid
        assert lc.orden, f"L{lid} sin orden de estaciones"
        assert lc.length > 0
        ds = [d for d, _s in lc.orden]
        assert ds == sorted(ds), f"L{lid}: orden no creciente"
        assert 0 <= ds[0] and ds[-1] <= lc.length + 1e-6
        ids = [s for _d, s in lc.orden]
        assert len(ids) == len(set(ids)), f"L{lid}: estación repetida"
        assert set(ids) == lc.estaciones
        assert all(s in net.estaciones for s in ids)
        base = lc.variante_de or lid      # una variante de ramal sirve a su línea
        assert all(base in net.estaciones[s].lineas for s in ids)
        assert lc.color == net.colores_comercial[str(base)]


def test_tranvias_sobre_trazado_osm(net):
    for lid in TRANVIAS:
        assert net.comerciales[lid].seg == f"tram:{lid}"
    for lid in set(LINEAS_COMERCIALES) - TRANVIAS:
        # en metro `seg` es la clave (int) de la polyline de vía elegida, aunque
        # el dataclass lo anote como str
        seg = net.comerciales[lid].seg
        assert isinstance(seg, int) and seg in net.lineas


@pytest.mark.parametrize("lid, primera, ultima", [
    (1, "Castelló", "Bétera"),
    (2, "Torrent Avinguda", "Llíria"),
    (3, "Aeroport", "Rafelbunyol"),
    (4, "Mas del Rosari", "Dr. Lluch"),
    (5, "Aeroport", "Marítim"),
    (7, "Torrent Avinguda", "Marítim"),
    (9, "Riba-roja de Túria", "Alboraia Peris Aragó"),
    (10, "Alacant", "Natzaret"),
])
def test_extremos_de_linea(net, lid, primera, ultima):
    nombres = [net.estaciones[s].nombre for _d, s in net.comerciales[lid].orden]
    assert nombres[0] == primera and nombres[-1] == ultima


# Ramal de Terramelar de L4: sus dos estaciones quedan fuera de la polyline de
# tranvía fusionada (OSM), a 500-650 m de la vía principal. Limitación conocida.
RAMAL_L4 = {(4, "Ll. Llarga - Terramelar"), (4, "Fira València")}


def test_estaciones_proyectadas_cerca_de_su_via(net):
    """dist_to_latlng(d de la estación) devuelve un punto próximo a la
    estación: la vía sobre la que se sitúan los trenes pasa por ella."""
    lejos = []
    for lid, lc in net.comerciales.items():
        for d, s in lc.orden:
            e = net.estaciones[s]
            la, ln = lc.dist_to_latlng(d)
            m = math.hypot((la - e.lat) * 111000, (ln - e.lng) * 85000)
            if m > 250:
                lejos.append((lid, e.nombre, round(m)))
    assert {(l, n) for l, n, _m in lejos} <= RAMAL_L4, lejos
    assert all(m < 700 for _l, _n, m in lejos), lejos


def test_station_dist_y_dist_to_latlng(net):
    lc = net.comerciales[3]
    assert lc.station_dist(net.id_por_nombre("Aeroport")) == 0.0 or \
        lc.station_dist(net.id_por_nombre("Aeroport")) < 50
    assert lc.station_dist(999999) is None
    assert lc.dist_to_latlng(-100) == lc.dist_to_latlng(0)             # recorta por abajo
    assert lc.dist_to_latlng(lc.length + 5000) == lc.dist_to_latlng(lc.length)


def test_id_por_nombre(net):
    assert net.id_por_nombre("Àngel Guimerà") == 25
    assert net.id_por_nombre("Torrent Avinguda") == 107
    assert net.id_por_nombre("Llíria") == 54
    assert net.id_por_nombre("Marítim") == 123
    assert net.id_por_nombre("No Existe") is None
    assert net.id_por_nombre("angel guimera") is None     # exacto, sin normalizar


def test_cabeceras_existen_en_la_red(net):
    for c in CABECERAS:
        assert c["id"] in net.estaciones, c
        assert net.estaciones[c["id"]].nombre == c["nombre"]
        for lid in c["lineas"]:
            # la cabecera puede estar en la principal o en una variante de ramal (Ll. Llarga)
            assert any(c["id"] in net.comerciales[v].estaciones for v in net.vias_de(lid)), c


def test_estaciones_retorno(net):
    ret = net.estaciones_retorno()
    assert ret == sorted(ret)
    assert {"València Sud", "Empalme", "Castelló", "Bétera", "Marítim", "Aeroport"} <= set(ret)


def test_orden_nombres_polyline_metro(net):
    assert net.orden_nombres(1)[0] == "Castelló"
    assert len(net.orden_nombres(1)) == 40


# ------------------------------------------------------------------ cierres

def _con_service(monkeypatch, tmp_path, cierres: list[dict]) -> Network:
    p = tmp_path / "service.json"
    p.write_text(json.dumps({"cierres": cierres, "subterraneos": [], "estaciones_retorno": []}),
                 encoding="utf-8")
    monkeypatch.setattr(network, "SERVICE_JSON", p)
    return load_network()


def test_cierres_descarta_los_vencidos(net, monkeypatch, tmp_path):
    hoy = date.today()
    ayer = (hoy - timedelta(days=1)).isoformat()
    manana = (hoy + timedelta(days=1)).isoformat()
    n = _con_service(monkeypatch, tmp_path, [
        {"id": "vencido", "tramo": ["Alameda", "Aragó"], "hasta": ayer},
        {"id": "hoy", "tramo": ["Alameda", "Aragó"], "hasta": hoy.isoformat()},
        {"id": "vigente", "tramo": ["Alameda", "Aragó"], "hasta": manana},
        {"id": "indefinido", "tramo": ["Alameda", "Aragó"]},
    ])
    assert [c["id"] for c in n.cierres()] == ["vigente", "indefinido"]
    assert [c["id"] for c in n.to_geojson_meta()["cierres"]] == ["vigente", "indefinido"]


def test_cierres_sin_service_json(net, monkeypatch, tmp_path):
    monkeypatch.setattr(network, "SERVICE_JSON", tmp_path / "no_existe.json")
    assert load_network().cierres() == []
    assert load_network().subterraneos() == []


def test_service_json_versionado_es_valido(net):
    """El service.json del repo se lee sin error y su cierre de obras de 2026
    (hasta 2026-09-01) ya no aparece como vigente."""
    assert "alameda-maritim-2026" not in {c.get("id") for c in net.cierres()}
    assert all("tramo" in s and "linea" in s for s in net.subterraneos())


# ---------------------------------------------------------------- geojson meta

def test_to_geojson_meta(net):
    meta = net.to_geojson_meta()
    assert set(meta) >= {"colores", "colores_comercial", "cierres", "subterraneos",
                         "estaciones_retorno", "conectores", "tram_tracks",
                         "tram_tracks_extra", "metro_segs", "comerciales", "lineas",
                         "estaciones"}
    assert len(meta["estaciones"]) == 144
    assert sorted(int(k) for k, c in meta["comerciales"].items()
                  if c["variante_de"] is None) == LINEAS_COMERCIALES
    for k, c in meta["comerciales"].items():
        assert len(c["path"]) == len(c["cum"]) >= 2
        assert c["estaciones"] and all({"id", "d"} <= set(e) for e in c["estaciones"])
        assert c["length"] >= c["cum"][-1] - 0.1
    assert set(meta["tram_tracks"]) == {str(l) for l in TRANVIAS}
    assert not any(str(s).startswith("tram:") for s in meta["metro_segs"])
    assert all(s in net.lineas for s in meta["metro_segs"])
    assert meta["sin_tiempo_real"] == []
    json.dumps(meta)     # serializable tal cual para /api/network


def test_conectores_no_tocan_tranvias(net):
    for c in net.conectores():
        assert c["linea"] not in TRANVIAS
        assert len(c["path"]) == 2


# ------------------------------------------------------------ ramales (L4)

def _metros(a, b):
    import math
    return math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 85000)


def test_l4_tiene_variantes_por_ramal(net):
    """La cadena OSM fusionada de L4 es una sola polyline: los ramales de Fira
    València y de Ll. Llarga - Terramelar van como variantes 41/42, que
    comparten el tronco (mismas d) con la principal."""
    assert net.vias_de(4) == [4, 41, 42]
    assert net.vias_de(1) == [1]
    for v in (41, 42):
        lc = net.comerciales[v]
        assert lc.variante_de == 4 and lc.color == net.comerciales[4].color
        assert 0 < lc.d_inicio < lc.d_union < lc.length
        assert lc.length == net.comerciales[4].length
        assert lc.cum == sorted(lc.cum)
        assert len(lc.latlng) == len(lc.cum)
    nombres = lambda v: [net.estaciones[s].nombre for _d, s in net.comerciales[v].orden]  # noqa: E731
    assert nombres(41)[0] == "Fira València" and nombres(42)[0] == "Ll. Llarga - Terramelar"
    assert nombres(41)[-1] == nombres(42)[-1] == nombres(4)[-1] == "Dr. Lluch"
    # las del ramal ya no están en la principal (se proyectaban a 600 m de su sitio)
    assert net.id_por_nombre("Ll. Llarga - Terramelar") not in net.comerciales[4].estaciones
    assert net.id_por_nombre("Fira València") not in net.comerciales[4].estaciones
    assert net.id_por_nombre("Mas del Rosari") in net.comerciales[4].estaciones


def test_estaciones_de_ramal_caen_sobre_su_estacion(net):
    for v, nombre in ((42, "Ll. Llarga - Terramelar"), (41, "Fira València")):
        lc = net.comerciales[v]
        sid = net.id_por_nombre(nombre)
        d = lc.station_dist(sid)
        e = net.estaciones[sid]
        assert d is not None and d < lc.d_union
        assert _metros(lc.dist_to_latlng(d), (e.lat, e.lng)) < 30


def test_tronco_comparte_d_entre_principal_y_variantes(net):
    main = net.comerciales[4]
    for v in (41, 42):
        lc = net.comerciales[v]
        for nombre in ("Empalme", "Benimaclet", "Dr. Lluch", "Cabanyal"):
            s = net.id_por_nombre(nombre)
            assert lc.station_dist(s) == main.station_dist(s), nombre
        # y la misma geometría en el tronco
        d = main.station_dist(net.id_por_nombre("Benimaclet"))
        assert _metros(lc.dist_to_latlng(d), main.dist_to_latlng(d)) < 1


def test_variante_no_extrapola_por_debajo_de_su_inicio(net):
    lc = net.comerciales[42]
    assert lc.dist_to_latlng(0) == lc.dist_to_latlng(lc.d_inicio)

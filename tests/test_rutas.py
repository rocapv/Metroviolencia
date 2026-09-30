"""backend/rutas.py: planificador (parseo y consulta cacheada) y trenes en vivo por paso."""
from __future__ import annotations

import datetime as dt
import re

import pytest

from backend import horarios, rutas
from backend.rutas import (_ts, consultar_ruta, desfases, enriquecer, lineas_paso,
                           parse_alternativas, sin_tren, trenes_paso)

TORRENT, LLIRIA, JESUS = 107, 54, 23     # Jesús: L2 y L7 en network.json
HHMM = re.compile(r"^\d{2}:\d{2}$")


# ------------------------------------------------------------ parseo real

def test_parse_ruta_directa(ruta_directa):
    alts = parse_alternativas(ruta_directa["html"])
    assert len(alts) == 1
    a = alts[0]
    assert a["transbordos"] == 0 and a["duracion_min"] == 71
    assert len(a["pasos"]) == 1
    p = a["pasos"][0]
    assert (p["de"], p["a"], p["transbordo"]) == ("Torrent Avinguda", "Llíria", False)
    assert p["destinos"] == ["Llíria"]
    assert p["salidas"][:2] == ["10:17", "10:47"]
    assert all(HHMM.match(h) for h in p["salidas"])
    assert p["salidas"] == sorted(p["salidas"])
    assert all("10:00" <= h <= "12:00" for h in p["salidas"])   # la ventana pedida


def test_parse_ruta_con_transbordo(ruta_transbordo):
    alts = parse_alternativas(ruta_transbordo["html"])
    assert len(alts) == 1
    a = alts[0]
    assert a["transbordos"] == 1 and a["duracion_min"] == 66
    p1, p2 = a["pasos"]
    assert (p1["de"], p1["a"], p1["transbordo"], p1["tiempo_min"]) == \
        ("Torrent Avinguda", "Àngel Guimerà", True, 21)
    assert set(p1["destinos"]) == {"Paterna", "Llíria"}
    assert (p2["de"], p2["a"], p2["transbordo"], p2["tiempo_min"]) == \
        ("Àngel Guimerà", "Rafelbunyol", False, 37)
    assert p2["destinos"] == ["Rafelbunyol"]
    assert p1["salidas"] and p2["salidas"]


def test_parse_sin_resultados():
    assert parse_alternativas("") == []
    assert parse_alternativas("<div>sin js-ruta-opcion</div>") == []


# ------------------------------------------------ consultar_ruta sin red

class _Resp:
    def __init__(self, html):
        self._html = html

    def raise_for_status(self):
        pass

    def json(self):
        return {"html": self._html}


class _Sesion:
    """Sustituto de requests.Session: devuelve la fixture y cuenta llamadas."""
    def __init__(self, html):
        self.html, self.llamadas = html, []

    def post(self, url, data=None, headers=None, timeout=None, verify=None):
        self.llamadas.append(data["data"])
        return _Resp(self.html)


def test_consultar_ruta_usa_el_planificador_y_cachea(monkeypatch, ruta_directa):
    monkeypatch.setattr(horarios, "_token", lambda s, h, v: "abc123")
    ses = _Sesion(ruta_directa["html"])
    res = consultar_ruta(ses, {"User-Agent": "t"}, True, TORRENT, LLIRIA)
    assert res["dia"] == dt.date.today().isoformat()
    assert res["ventana"] == "2h"
    assert len(res["alternativas"]) == 1
    assert res["alternativas"][0]["pasos"][0]["a"] == "Llíria"
    assert len(ses.llamadas) == 1
    assert f"origen={TORRENT}&destino={LLIRIA}" in ses.llamadas[0]
    assert "auth_token=abc123" in ses.llamadas[0]
    # segunda consulta idéntica dentro del TTL: de caché, sin tocar la web
    assert consultar_ruta(ses, {"User-Agent": "t"}, True, TORRENT, LLIRIA) is res
    assert len(ses.llamadas) == 1


def test_consultar_ruta_sin_trenes_amplia_a_manana(monkeypatch):
    monkeypatch.setattr(horarios, "_token", lambda s, h, v: "tok")
    ses = _Sesion("")            # el planificador no devuelve nada
    res = consultar_ruta(ses, {"User-Agent": "t"}, True, TORRENT, LLIRIA)
    assert res["ventana"] == "manana"
    assert res["dia"] == (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert res["alternativas"] == []
    # cada ventana reintenta con token nuevo cuando no hay alternativas: >= 3 peticiones
    assert len(ses.llamadas) >= 3


# ------------------------------------------------------ líneas y trenes

def test_lineas_paso(net):
    assert lineas_paso(net, TORRENT, LLIRIA, set()) == [2]
    assert lineas_paso(net, TORRENT, LLIRIA, {2}) == [2]
    # Torrent Avinguda -> Jesús: L2 y L7 pasan por ambas; si el tiempo real
    # solo ve L7 en la de subida, solo L7
    assert net.comerciales[7].estaciones & net.comerciales[2].estaciones >= {TORRENT, JESUS}
    assert lineas_paso(net, TORRENT, JESUS, set()) == [2, 7]
    assert lineas_paso(net, TORRENT, JESUS, {7}) == [7]
    assert lineas_paso(net, TORRENT, JESUS, {9}) == [2, 7]       # L9 no pasa: se ignora
    assert lineas_paso(net, 1, LLIRIA, set()) == []              # Castelló (L1) y Llíria (L2)


def _tren(id_, net, linea, destino, ds: list[float], noms=None):
    wps = [{"d": ds[0], "eta": 0}] + [
        {"d": d, "eta": 120 * (i + 1), "nom": (noms or [""] * len(ds))[i + 1]}
        for i, d in enumerate(ds[1:])]
    return {"id": id_, "linea": linea, "destino": destino, "waypoints": wps}


def test_trenes_paso_filtra_por_sentido_posicion_y_destino(net):
    lc = net.comerciales[2]
    o = [s for _d, s in lc.orden]
    d_s, d_t = lc.station_dist(TORRENT), lc.station_dist(LLIRIA)
    assert d_s < d_t
    d1, d2 = lc.station_dist(o[1]), lc.station_dist(o[2])
    trenes = [
        _tren(1, net, 2, "Llíria", [d_s, d1, d2]),                 # en cabecera, sirve
        _tren(2, net, 2, "Llíria", [d_s + 2000, d2]),              # ya pasó por Torrent
        _tren(3, net, 2, "Torrent Avinguda", [d2, d1, d_s]),       # sentido contrario
        _tren(4, net, 2, "Paterna", [d_s - 50, d1, d2]),           # se queda antes de Llíria
        _tren(5, net, 7, "Marítim", [d_s, d1]),                    # otra línea
        _tren(6, net, 2, "Llíria", [d_s - 100, d1, d2]),           # detrás del 1, sirve
    ]
    llegadas_s = [(2, "Llíria", 120), (7, "Marítim", 300), (2, "Llíria", 1500)]
    out = trenes_paso(net, trenes, 2, TORRENT, LLIRIA, llegadas_s)
    assert [t["id"] for t in out] == [1, 6]
    assert [t["eta_subida"] for t in out] == [120, 1500]
    assert all(t["linea"] == 2 and t["destino"] == "Llíria" for t in out)
    # sin línea válida o mismo origen/destino
    assert trenes_paso(net, trenes, 99, TORRENT, LLIRIA, []) == []
    assert trenes_paso(net, trenes, 2, TORRENT, TORRENT, []) == []


def test_desfases():
    dia = "2030-01-01"
    ts_datos = _ts(dia, "10:15")
    trenes_p = [{"id": 1, "eta_subida": 120}, {"id": 2, "eta_subida": 300},
                {"id": 3, "eta_subida": None}, {"id": 4, "eta_subida": 3600}]
    desfases(trenes_p, ["10:17", "10:47"], dia, ts_datos)
    por = {t["id"]: (t["hora_prog"], t["desfase_s"]) for t in trenes_p}
    assert por[1] == ("10:17", 0)
    assert por[2] == ("10:47", -1620) or por[2] == (None, None)   # 10:17 ya usada por el 1
    assert por[3] == (None, None)
    assert por[4] == (None, None)          # "1 h" sin minutos: no mide desfase


def test_sin_tren_detecta_salida_sin_tren_anunciado():
    dia = "2030-01-01"
    ts_datos = _ts(dia, "10:15")
    ahora = float(ts_datos)
    # 10:17 casa con el tren anunciado; 10:40 (dentro de VENTANA_CANC=30 min y
    # antes del último anunciado) no tiene tren a <15 min; 11:17 queda más
    # allá del último anunciado: aún no se juzga
    p = {"trenes": [{"destino": "Llíria"}], "destinos": ["Llíria"], "lineas": [2],
         "salidas": ["10:17", "10:40", "11:17"]}
    llegadas_s = [(2, "Llíria", 120), (2, "Llíria", 3000)]     # 10:17 y 11:05 anunciados
    assert sin_tren(p, llegadas_s, dia, ts_datos, ahora) == ["10:40"]
    # sin ningún tren anunciado no se juzga nada
    assert sin_tren(p, [], dia, ts_datos, ahora) == []
    assert sin_tren(dict(p, trenes=[], destinos=[]), llegadas_s, dia, ts_datos, ahora) == []


def test_enriquecer_ruta_directa(net, ruta_directa):
    """Caso conocido: Torrent Avinguda -> Llíria por L2, con un tren en vivo en
    la cabecera que casa con la salida programada de las 10:17."""
    alts = parse_alternativas(ruta_directa["html"])
    lc = net.comerciales[2]
    o = [s for _d, s in lc.orden]
    d_s = lc.station_dist(TORRENT)
    tren = _tren(11, net, 2, "Llíria", [d_s, lc.station_dist(o[1]), lc.station_dist(o[2])],
                 noms=["", net.estaciones[o[1]].nombre, net.estaciones[o[2]].nombre])
    dia = "2030-01-01"                            # todas las salidas quedan en el futuro
    ts_datos = _ts(dia, "10:15")
    llegadas = {TORRENT: [(2, "Llíria", 120), (2, "Paterna", 600)]}
    out = enriquecer(net, alts, [tren], llegadas, dia, ts_datos)
    p = out[0]["pasos"][0]
    assert (p["de_id"], p["a_id"]) == (TORRENT, LLIRIA)
    assert p["lineas"] == [2]
    assert p["proxima_prog"] == "10:17"
    assert len(p["trenes"]) == 1
    t = p["trenes"][0]
    assert (t["id"], t["linea"], t["destino"], t["eta_subida"]) == (11, 2, "Llíria", 120)
    assert (t["hora_prog"], t["desfase_s"]) == ("10:17", 0)
    assert p["sin_tren"] == []


def test_enriquecer_estacion_desconocida_no_rompe(net):
    alts = [{"duracion_min": 5, "transbordos": 0,
             "pasos": [{"de": "Nowhere", "a": "Llíria", "transbordo": False,
                        "tiempo_min": 5, "destinos": [], "salidas": []}]}]
    out = enriquecer(net, alts, [], {})
    p = out[0]["pasos"][0]
    assert p["de_id"] is None and p["a_id"] == LLIRIA
    assert p["lineas"] == [] and p["trenes"] == [] and p["proxima_prog"] is None


@pytest.mark.parametrize("hhmm, minutos", [("00:00", 0), ("10:17", 617), ("23:59", 1439)])
def test_ts_dia_hora(hhmm, minutos):
    assert (_ts("2030-01-01", hhmm) - _ts("2030-01-01", "00:00")) == minutos * 60

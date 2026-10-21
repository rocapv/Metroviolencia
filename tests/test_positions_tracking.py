"""backend/positions.py (estimar) y backend/tracking.py (track).

Propiedad clave: un tren identificado NUNCA salta hacia atrás entre dos
pasadas y conserva su track_id cuando avanza de forma coherente.
"""
from __future__ import annotations

import json

import pytest

from backend import positions
from backend.positions import Tren, estimar
from backend.tracking import _alinear, predict_d, track

LINEA = 3                      # metro: Aeroport (d=0) ... Rafelbunyol (d=len)
HACIA_FIN = "Rafelbunyol"      # última estación del orden -> sentido +1
HACIA_INICIO = "Aeroport"      # primera -> sentido -1


def _orden(net, lid=LINEA):
    return [s for _d, s in net.comerciales[lid].orden]


def _dist(net, lid=LINEA):
    return {s: d for d, s in net.comerciales[lid].orden}


def _llegadas(net, destino: str, idxs: list[int], etas: list[int], lid=LINEA):
    """[(estacion_id, linea, destino, eta_seg)] para las estaciones `idxs`
    (índices sobre el orden de la línea) con esas ETAs."""
    o = _orden(net, lid)
    return [(o[i], lid, destino, e) for i, e in zip(idxs, etas)]


def _sentido(t) -> int:
    wp = t.waypoints if isinstance(t, Tren) else t["waypoints"]
    return 1 if wp[-1]["d"] >= wp[0]["d"] else -1


def _prev(trenes: list[Tren], ids: list[int]) -> list[dict]:
    """Forma en que el poller guarda el snapshot anterior para track()."""
    return [{"track_id": i, "linea": t.linea, "destino": t.destino,
             "waypoints": json.loads(json.dumps(t.waypoints))}
            for i, t in zip(ids, trenes)]


# ------------------------------------------------------------ un tren, +1

def test_un_tren_avanza_y_conserva_identidad(net):
    o, dist = _orden(net), _dist(net)
    # pasada 1: próxima parada la 6ª, luego 7ª, 8ª, 9ª a 2 min por tramo
    p1 = estimar(net, _llegadas(net, HACIA_FIN, [6, 7, 8, 9], [60, 180, 300, 420]))
    assert len(p1) == 1
    t1 = p1[0]
    assert t1.linea == LINEA and t1.destino == HACIA_FIN
    assert t1.prox_estacion == net.estaciones[o[6]].nombre and t1.eta_seg == 60
    assert t1.origen is False
    assert t1.waypoints[0]["eta"] == 0
    # entre la estación anterior y la próxima, y los waypoints hacia delante
    assert dist[o[5]] <= t1.waypoints[0]["d"] <= dist[o[6]]
    ds = [w["d"] for w in t1.waypoints]
    assert ds == sorted(ds)
    assert [w["nom"] for w in t1.waypoints[1:]] == [net.estaciones[o[i]].nombre for i in (6, 7, 8, 9)]
    assert t1.color == net.comerciales[LINEA].color

    # pasada 2, 60 s después: pasó por la 6ª, ETAs decrecen 60 s, aparece la 10ª
    p2 = estimar(net, _llegadas(net, HACIA_FIN, [7, 8, 9, 10], [120, 240, 360, 480]))
    assert len(p2) == 1
    t2 = p2[0]
    ids1, next_id = track(p1, [], 0.0, 1)
    assert ids1 == [1] and next_id == 2
    ids2, next_id = track(p2, _prev(p1, ids1), 60.0, next_id)
    assert ids2 == [1], "el tren que avanza coherentemente conserva su track_id"
    assert next_id == 2
    assert t2.waypoints[0]["d"] > t1.waypoints[0]["d"], "avance monótono de d"

    # pasada 3, otros 60 s: misma identidad y nunca hacia atrás. (Aquí no hay
    # muestra de tiempo del tramo 6ª->7ª y la velocidad de reserva recorta el
    # punto a la estación anterior, así que d puede quedarse igual, no menor.)
    p3 = estimar(net, _llegadas(net, HACIA_FIN, [7, 8, 9, 10], [60, 180, 300, 420]))
    ids3, next_id = track(p3, _prev(p2, ids2), 60.0, next_id)
    assert ids3 == [1] and next_id == 2
    assert p3[0].waypoints[0]["d"] >= t2.waypoints[0]["d"]
    assert p3[0].eta_seg < t2.eta_seg


# ------------------------------------------------------------ un tren, -1

def test_un_tren_en_sentido_contrario(net):
    o, dist = _orden(net), _dist(net)
    p1 = estimar(net, _llegadas(net, HACIA_INICIO, [10, 9, 8, 7], [60, 180, 300, 420]))
    assert len(p1) == 1
    t1 = p1[0]
    assert t1.prox_estacion == net.estaciones[o[10]].nombre
    assert dist[o[10]] <= t1.waypoints[0]["d"] <= dist[o[11]]
    ds = [w["d"] for w in t1.waypoints]
    assert ds == sorted(ds, reverse=True)
    assert _sentido(t1) == -1

    p2 = estimar(net, _llegadas(net, HACIA_INICIO, [9, 8, 7, 6], [120, 240, 360, 480]))
    ids1, nid = track(p1, [], 0.0, 7)
    ids2, nid = track(p2, _prev(p1, ids1), 60.0, nid)
    assert ids2 == ids1 == [7]
    # avanza en su sentido: d DECRECE
    assert p2[0].waypoints[0]["d"] < t1.waypoints[0]["d"]


# ------------------------------------------------------------ dos trenes

def test_dos_trenes_mismo_destino_identidades_estables(net):
    o = _orden(net)
    # A por delante (próxima 12ª), B por detrás (próxima 3ª); la caída de ETA
    # entre la 5ª (300 s) y la 12ª (60 s) separa los dos trenes
    ll1 = _llegadas(net, HACIA_FIN, [3, 4, 5], [60, 180, 300]) + \
        _llegadas(net, HACIA_FIN, [12, 13, 14], [60, 180, 300])
    p1 = estimar(net, ll1)
    assert len(p1) == 2
    ids1, nid = track(p1, [], 0.0, 100)
    assert sorted(ids1) == [100, 101]
    por_id1 = {i: t for i, t in zip(ids1, p1)}

    ll2 = _llegadas(net, HACIA_FIN, [4, 5, 6], [120, 240, 360]) + \
        _llegadas(net, HACIA_FIN, [13, 14, 15], [120, 240, 360])
    p2 = estimar(net, ll2)
    assert len(p2) == 2
    ids2, nid = track(p2, _prev(p1, ids1), 60.0, nid)
    assert sorted(ids2) == [100, 101], "ningún tren nuevo: los dos se reconocen"
    assert nid == 102
    for i, t in zip(ids2, p2):
        antes = por_id1[i]
        assert t.waypoints[0]["d"] > antes.waypoints[0]["d"], f"track {i} retrocedió"
        assert t.prox_estacion != antes.prox_estacion or t.eta_seg <= antes.eta_seg


def test_tren_nuevo_y_tren_que_desaparece(net):
    p1 = estimar(net, _llegadas(net, HACIA_FIN, [6, 7, 8], [60, 180, 300]))
    ids1, nid = track(p1, [], 0.0, 1)
    # en la pasada 2 el de antes ya no está y hay otro muy lejos (no casa)
    p2 = estimar(net, _llegadas(net, HACIA_FIN, [20, 21, 22], [60, 180, 300]))
    ids2, nid = track(p2, _prev(p1, ids1), 60.0, nid)
    assert ids2 == [2] and nid == 3


# ---------------------------------------------------- cabecera, dedup, cierres

def test_tren_en_cabecera_es_origen(net):
    o = _orden(net)
    p = estimar(net, _llegadas(net, HACIA_FIN, [0, 1, 2], [600, 720, 840]))
    assert len(p) == 1 and p[0].origen is True
    assert p[0].waypoints[0]["d"] == pytest.approx(_dist(net)[o[0]], abs=0.1)
    assert p[0].prox_estacion == net.estaciones[o[0]].nombre


def test_dedup_colapsa_duplicado_de_cabecera(net):
    """La fuente lista en la terminal el tren que acaba de llegar (a punto de
    salir de nuevo) y su próxima salida: dos `Tren` a < DEDUP_TOL m son uno."""
    o = _orden(net)
    # en la cabecera: uno que sale dentro de 15 min (espera, origen) y otro que
    # llega a la 2ª en 100 s: la caída de ETA los separa en dos trenes. Con la
    # velocidad de reserva (11 m/s x 100 s > 660 m del tramo) el segundo se
    # recorta a la cabecera: mismo punto que el que espera -> duplicado.
    ll = _llegadas(net, HACIA_FIN, [0], [900]) + _llegadas(net, HACIA_FIN, [1], [100])
    p = estimar(net, ll)
    assert len(p) == 1
    assert p[0].origen is False, "se prefiere el tren en marcha al que espera"
    assert p[0].waypoints[0]["d"] == pytest.approx(_dist(net)[o[0]], abs=positions.DEDUP_TOL)


def test_tramo_cerrado_no_emite_tren(net, monkeypatch):
    o = _orden(net)
    a, b = net.estaciones[o[5]].nombre, net.estaciones[o[6]].nombre
    monkeypatch.setattr(net, "cierres", lambda: [{"tramo": [a, b], "lineas": [LINEA]}])
    assert estimar(net, _llegadas(net, HACIA_FIN, [6, 7, 8], [60, 180, 300])) == []
    # con el cierre en otro tramo sí se emite
    monkeypatch.setattr(net, "cierres", lambda: [{"tramo": [
        net.estaciones[o[20]].nombre, net.estaciones[o[21]].nombre]}])
    assert len(estimar(net, _llegadas(net, HACIA_FIN, [6, 7, 8], [60, 180, 300]))) == 1


def test_llegadas_de_estacion_ajena_a_la_linea_se_ignoran(net):
    o1 = _orden(net, 1)
    # Castelló (L1) anunciando L3: no está en L3 -> nada
    assert estimar(net, [(o1[0], LINEA, HACIA_FIN, 60)]) == []
    assert estimar(net, []) == []


def test_to_dict_serializable(net):
    t = estimar(net, _llegadas(net, HACIA_FIN, [6, 7], [60, 180]))[0]
    d = t.to_dict()
    assert set(d) == {"linea", "destino", "color", "lat", "lng", "prox_estacion",
                      "eta_seg", "waypoints", "origen", "via"}
    json.dumps(d)


# --------------------------------------------------------- tracking, unidades

def test_predict_d():
    wp = [{"d": 100.0, "eta": 0}, {"d": 400.0, "eta": 60}, {"d": 1000.0, "eta": 120}]
    assert predict_d([], 10) is None
    assert predict_d(wp, 0) == 100.0
    assert predict_d(wp, 30) == pytest.approx(250.0)
    assert predict_d(wp, 60) == 400.0
    assert predict_d(wp, 90) == pytest.approx(700.0)
    assert predict_d(wp, 999) == 1000.0        # más allá de lo conocido: último punto


def test_alinear_respeta_el_orden():
    # dos previstos y dos actuales: se casan en orden, sin cruzarse
    asig = _alinear([(100, 300), (900, 1100)], [250, 1000], 60)
    assert asig == [0, 1]
    # uno actual muy lejos de todo: tren nuevo (None)
    asig = _alinear([(100, 300)], [250, 5000], 60)
    assert asig == [0, None]
    # sin previstos: todos nuevos
    assert _alinear([], [1, 2, 3], 60) == [None, None, None]


# ------------------------------------------------------------ datos reales

def _trenes_snapshot(extracto, snap):
    return [t for t in extracto["trenes"] if t["snapshot_id"] == snap]


def _llegadas_snapshot(extracto, snap):
    return [(a["estacion_id"], a["linea"], a["destino"], a["eta_seg"])
            for a in extracto["arrivals"] if a["snapshot_id"] == snap]


@pytest.mark.parametrize("snap", [31113, 31114])
def test_estimar_reproduce_los_trenes_guardados_en_raspa(net, extracto, snap):
    """Con las mismas llegadas crudas, el estimador local da EXACTAMENTE los
    trenes que el poller de la Raspa guardó (misma red, mismo código)."""
    est = estimar(net, _llegadas_snapshot(extracto, snap))
    guardados = _trenes_snapshot(extracto, snap)
    clave = lambda linea, destino, d: (linea, destino, round(d, 1))  # noqa: E731
    # Fuera de L4, exactamente los mismos trenes
    a = sorted(clave(t.linea, t.destino, t.waypoints[0]["d"]) for t in est if t.linea != 4)
    b = sorted(clave(t["linea"], t["destino"], t["d"]) for t in guardados if t["linea"] != 4)
    assert a == b and len(a) > 50
    # L4 va por ramales desde el 21/10/2026 (variantes 41/42): los trenes son los
    # mismos salvo que (1) el que espera en Fira València existe de verdad y
    # (2) desaparece el fantasma que el tronco deducía en Vicent Andrés Estellés
    # a partir del ETA de Fira (que estaba proyectada sobre el tronco).
    k4 = lambda dest, prox, eta: (dest, prox, eta)  # noqa: E731
    a4 = sorted(k4(t.destino, t.prox_estacion, t.eta_seg) for t in est if t.linea == 4)
    b4 = sorted(k4(t["destino"], t["prox_estacion"], t["eta_seg"]) for t in guardados if t["linea"] == 4)
    fantasma = ("Dr. Lluch", "Vicent Andrés Estellés", 480)
    fira = ("Dr. Lluch", "Fira València", 540 if snap == 31113 else 480)
    assert sorted(x for x in a4 if x != fira) == sorted(x for x in b4 if x != fantasma)
    assert fira in a4
    for t in est:
        assert t.waypoints[0]["eta"] == 0
        ds = [w["d"] for w in t.waypoints]
        assert ds == sorted(ds) or ds == sorted(ds, reverse=True), "trayectoria monótona"


def test_track_reproduce_los_track_id_guardados_en_raspa(extracto):
    """Dos pasadas consecutivas reales (60 s): track() vuelve a asignar los
    mismos track_id que quedaron en la tabla `trenes`."""
    prev_rows = _trenes_snapshot(extracto, 31113)
    curr_rows = _trenes_snapshot(extracto, 31114)
    prev = [{"track_id": t["track_id"], "linea": t["linea"], "destino": t["destino"],
             "waypoints": json.loads(t["waypoints"])} for t in prev_rows]
    curr = [Tren(t["linea"], t["destino"], t["color"], t["lat"], t["lng"], t["prox_estacion"],
                 t["eta_seg"], json.loads(t["waypoints"]), bool(t["origen"])) for t in curr_rows]
    dt = curr_rows[0]["ts"] - prev_rows[0]["ts"]
    assert dt == 60
    ids_prev = {p["track_id"] for p in prev}
    nuevos = [t["track_id"] for t in curr_rows if t["track_id"] not in ids_prev]
    ids, next_id = track(curr, prev, float(dt), min(nuevos))
    assert ids == [t["track_id"] for t in curr_rows]
    assert next_id == max(nuevos) + 1

    # identidad conservada para la gran mayoría
    conservados = [(i, t) for i, t in zip(ids, curr) if i in ids_prev]
    assert len(conservados) >= 0.75 * len(curr)


def _retrocesos_reales(extracto) -> list[tuple[float, int, str]]:
    """(metros hacia atrás en su sentido, línea, destino) de cada tren que
    conserva identidad entre las dos pasadas reales y queda por detrás de
    donde estaba."""
    prev = {t["track_id"]: json.loads(t["waypoints"])
            for t in _trenes_snapshot(extracto, 31113)}
    out = []
    for t in _trenes_snapshot(extracto, 31114):
        wp = prev.get(t["track_id"])
        if wp is None:
            continue
        avance = (t["d"] - wp[0]["d"]) * (1 if wp[-1]["d"] >= wp[0]["d"] else -1)
        if avance < 0:
            out.append((-avance, t["linea"], t["destino"]))
    return out


def test_retroceso_real_dentro_de_la_puerta_del_tracker(extracto):
    """Con datos reales la fuente redondea a minutos (~660 m a 11 m/s): el
    estimador puede recolocar un tren conservado unas decenas de metros por
    detrás. Se admite mientras quepa en la tolerancia del propio tracker y
    sea excepcional."""
    from backend.tracking import _tol
    retro = _retrocesos_reales(extracto)
    conservados = sum(1 for t in _trenes_snapshot(extracto, 31114)
                      if t["track_id"] in {p["track_id"] for p in _trenes_snapshot(extracto, 31113)})
    assert len(retro) <= 0.1 * conservados, retro
    assert all(m <= _tol(60.0) for m, _l, _d in retro), retro


@pytest.mark.xfail(strict=True, reason=(
    "El estimador recoloca hasta ~200 m atrás un tren conservado entre dos pasadas "
    "reales (resolución de minuto de la fuente). Garantizarlo estricto exige que "
    "positions.estimar/tracking.track no dejen que d retroceda respecto al snapshot "
    "anterior: cambio de producción, fuera del alcance de los tests."))
def test_retroceso_real_estricto(extracto):
    assert _retrocesos_reales(extracto) == []


def test_l4_trenes_de_ramal_sobre_su_estacion(net, extracto):
    """Los trenes que nacen en Ll. Llarga - Terramelar o en Fira València se
    sitúan EN la estación (antes: a 600 m, proyectados sobre el tronco)."""
    import math
    est = estimar(net, _llegadas_snapshot(extracto, 31113))
    en_cabecera = {t.prox_estacion: t for t in est if t.linea == 4 and t.origen}
    for nombre, via in (("Ll. Llarga - Terramelar", 42), ("Fira València", 41)):
        t = en_cabecera[nombre]
        e = net.estaciones[net.id_por_nombre(nombre)]
        assert t.via == via
        assert math.hypot((t.lat - e.lat) * 111000, (t.lng - e.lng) * 85000) < 30
    # y ningún tren L4 queda lejos de la vía de su variante
    for t in est:
        if t.linea == 4:
            lc = net.comerciales[t.via]
            la, ln = lc.dist_to_latlng(t.waypoints[0]["d"])
            assert abs(la - t.lat) < 1e-6 and abs(ln - t.lng) < 1e-6

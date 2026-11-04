"""tracking.relleus i la regla de l'eixida en track (04/11/2026, inventari de flota)."""
from types import SimpleNamespace

from backend.tracking import relleus, track


def _d(net, linea, nom):
    sid = net.id_por_nombre(nom)
    return next(d for d, s in net.comerciales[linea].orden if s == sid)


def _nou(tid, linea, d, dir_, prox, origen=False, destino="Aeroport"):
    return {"track_id": tid, "linea": linea, "destino": destino, "via": linea, "d": d, "dir": dir_,
            "origen": origen, "cabecera": prox, "prox": prox}


def test_el_que_ix_de_la_capcalera_hereta_el_que_va_arribar(net):
    d = _d(net, 3, "Rafelbunyol")                    # L3: Rafelbunyol és el final (d alta)
    pend = [{"track_id": 7, "linea": 3, "destino": "Rafelbunyol", "ts": 1000}]
    nous = [_nou(50, 3, d - 600, -1, "Pobla de Farnals")]      # ix cap a Aeroport
    assert relleus(net, pend, nous) == {50: 7}


def test_lluny_o_cap_a_la_capcalera_no(net):
    d = _d(net, 3, "Rafelbunyol")
    pend = [{"track_id": 7, "linea": 3, "destino": "Rafelbunyol", "ts": 1000}]
    assert relleus(net, pend, [_nou(50, 3, d - 6000, -1, "Museros")]) == {}
    # el que hi va (s'acosta) no és el que n'ix
    assert relleus(net, pend, [_nou(51, 3, d - 600, 1, "Rafelbunyol", destino="Rafelbunyol")]) == {}


def test_dibuixat_darrere_amb_la_capcalera_com_a_proxima_parada(net):
    # Seminari: l'estimador posa el que n'ix 2,5 km darrere, «venint» de Masies
    d = _d(net, 1, "Seminari - CEU")
    pend = [{"track_id": 7, "linea": 1, "destino": "Seminari - CEU", "ts": 1000}]
    dir_ = -1 if _d(net, 1, "Castelló") < d else 1    # cap a Castelló
    assert relleus(net, pend, [_nou(50, 1, d - 2500 * dir_, dir_, "Seminari - CEU", destino="Castelló")]) == {50: 7}
    assert relleus(net, pend, [_nou(51, 1, d - 2500 * dir_, dir_, "Masies", destino="Castelló")]) == {}


def test_el_primer_que_arriba_es_el_primer_que_ix(net):
    d = _d(net, 3, "Rafelbunyol")
    pend = [{"track_id": 8, "linea": 3, "destino": "Rafelbunyol", "ts": 1300},
            {"track_id": 7, "linea": 3, "destino": "Rafelbunyol", "ts": 1000}]
    nous = [_nou(50, 3, d - 300, -1, "Pobla de Farnals")]
    assert relleus(net, pend, nous) == {50: 7}


def test_espera_en_capcalera(net):
    pend = [{"track_id": 7, "linea": 3, "destino": "Rafelbunyol", "ts": 1000}]
    nous = [_nou(50, 3, _d(net, 3, "Rafelbunyol"), -1, "Rafelbunyol", origen=True),
            _nou(51, 3, 0.0, 1, "Aeroport", origen=True)]
    assert relleus(net, pend, nous) == {50: 7}


def _t(d, origen, eta, destino="Aeroport"):
    return SimpleNamespace(linea=3, destino=destino, via=3, origen=origen, eta_seg=eta,
                           waypoints=[{"d": d, "eta": 0}, {"d": d, "eta": eta}, {"d": d - 900, "eta": eta + 90}])


def test_l_espera_no_es_queda_el_numero_del_que_ix():
    # pasada anterior: espera a la capçalera (d alta) amb eixida en 600 s
    prev = [{"track_id": 1, "linea": 3, "destino": "Aeroport", "via": 3, "origen": True, "eta": 600,
             "waypoints": [{"d": 20000, "eta": 0}, {"d": 20000, "eta": 600}, {"d": 19100, "eta": 690}]}]
    # ara: el tren ja ha eixit (FGV ho deia tard) i la capçalera anuncia la següent, d'ací 900 s
    curr = [_t(19700, False, 60), _t(20000, True, 900)]
    ids, _ = track(curr, prev, 120.0, 10)
    assert ids[0] == 1 and ids[1] == 10


def test_la_mateixa_eixida_conserva_el_numero():
    prev = [{"track_id": 1, "linea": 3, "destino": "Aeroport", "via": 3, "origen": True, "eta": 600,
             "waypoints": [{"d": 20000, "eta": 0}, {"d": 20000, "eta": 600}, {"d": 19100, "eta": 690}]}]
    ids, _ = track([_t(20000, True, 500)], prev, 120.0, 10)
    assert ids == [1]

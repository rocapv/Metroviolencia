"""backend/pasos.py: retrasos en ruta (horario de paso vs pasos reales)."""
from __future__ import annotations

import datetime as dt
import statistics

import pytest

from backend import pasos
from backend.pasos import (MARGEN_AMBIG, VENTANA, _crudos, _vecina, cancelados_estacion,
                           pares, pares_incoherentes, resumen_lineas, retraso_arribada,
                           retraso_vivo, retrasos, sentidos_inferidos)
from backend.salidas import _rango_dia

DIA = "2026-03-10"          # martes, sin cambio de hora cerca
L1, BETERA, CASTELLO = 1, "Bétera", "Castelló"


def _orden(net, lid=L1):
    return [s for _d, s in net.comerciales[lid].orden]


def _fila(s, v, r, **kw):
    return {"estacion_id": s, "hacia_id": v, "retraso_s": r, "destino": kw.get("destino", "X"),
            "linea": 1, "ts_paso": 0, "ts_prog": 0, "estacion": ""}


# ------------------------------------------------------- pares_incoherentes

def _cadena_con_salto():
    """Tramo 1->2->3->4->5->6 con retraso ~60 s en todas las paradas salvo la
    3->4, que da 5 min: es el horario de esa parada el que falla. (Los vecinos
    2->3 y 4->5 ven mediana 180 s: a 120 s de la suya, dentro de INCOHERENTE.)"""
    filas = []
    for s, v, r in ((1, 2, 60), (2, 3, 60), (3, 4, 300), (4, 5, 60), (5, 6, 60)):
        filas += [_fila(s, v, r + k) for k in range(8)]
    return filas


def test_pares_incoherentes_detecta_la_parada_que_salta():
    assert pares_incoherentes(_cadena_con_salto()) == {(3, 4)}


def test_pares_incoherentes_respeta_minimo_y_extremos():
    filas = _cadena_con_salto()
    assert pares_incoherentes(filas, minimo=9) == set()        # pocas muestras: nada
    assert pares_incoherentes(filas, extremos={3}) == set()     # donde arrancan trenes, se admite
    assert pares_incoherentes(filas, extremos={4}) == {(3, 4)}  # el extremo es la estación, no la vecina
    assert pares_incoherentes([]) == set()


def test_pares_incoherentes_coherentes_no_marca_nada():
    filas = []
    for s, v in ((1, 2), (2, 3), (3, 4)):
        filas += [_fila(s, v, 90 + 10 * k) for k in range(10)]
    assert pares_incoherentes(filas) == set()


def test_resumen_lineas():
    filas = [dict(_fila(1, 2, r), linea=l, ts_paso=t) for l, r, t in
             ((1, 30, 100), (1, 200, 200), (1, -20, 300), (4, 500, 100))]
    r = resumen_lineas(filas)
    assert r == {1: {"n": 3, "mediana_s": 30, "tarde": 1}, 4: {"n": 1, "mediana_s": 500, "tarde": 1}}
    assert resumen_lineas(filas, desde=150) == {1: {"n": 2, "mediana_s": 200, "tarde": 1}}


# ------------------------------------------------------------------ _vecina

def test_vecina_segun_destino(net):
    o = _orden(net)
    i = 10
    assert _vecina(net, L1, o[i], BETERA) == o[i + 1]
    assert _vecina(net, L1, o[i], CASTELLO) == o[i - 1]
    assert _vecina(net, L1, o[-1], BETERA) is None            # ya está en el destino
    assert _vecina(net, L1, o[0], CASTELLO) is None
    assert _vecina(net, 99, o[i], BETERA) is None
    assert _vecina(net, 3, o[0], BETERA) is None              # Castelló no está en L3


def test_vecina_destino_fuera_del_trazado_usa_sentido(net):
    """L1 rotulada 'Benimaclet' (no está en su trazado): solo con sentido."""
    o = _orden(net)
    assert net.id_por_nombre("Benimaclet") not in o
    assert _vecina(net, L1, o[10], "Benimaclet") is None
    assert _vecina(net, L1, o[10], "Benimaclet", sentido=1) == o[11]
    assert _vecina(net, L1, o[10], "Benimaclet", sentido=-1) == o[9]


def test_pares_cubre_ambos_sentidos(net):
    ps = pares(net)
    o = _orden(net)
    assert (o[0], o[1]) in ps and (o[1], o[0]) in ps
    assert len(ps) == len(set(ps)) and ps == sorted(ps)
    assert 250 <= len(ps) <= 320


# ------------------------------------------------ casamiento con BD sintética

@pytest.fixture
def escenario(net, bd):
    """Horario de paso y pasos reales en una L1 sintética del día DIA."""
    con = bd.con
    o = _orden(net)
    t0, _t1 = _rango_dia(DIA)
    A, B = o[10], o[11]             # A hacia Bétera -> vecina B
    C, D = o[20], o[21]
    E, F = o[30], o[31]
    h = lambda hh, mm: t0 + hh * 3600 + mm * 60  # noqa: E731
    prog = [
        (A, B, h(10, 0), "10:00"), (A, B, h(10, 30), "10:30"),
        (C, D, h(11, 0), "11:00"), (C, D, h(11, 1), "11:01"),   # tronco: dos a 1 min
        (E, F, h(12, 0), "12:00"),
    ]
    con.executemany("INSERT INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                    " VALUES (?,?,?,?,?)", [(DIA, *p) for p in prog])
    obs = [
        (A, L1, BETERA, h(10, 2)),                 # 2 min tarde sobre las 10:00
        (C, L1, BETERA, h(11, 0) + 30),            # ambiguo: 11:00 y 11:01 casi igual de cerca
        (E, L1, BETERA, h(12, 20)),                # a 20 min: fuera de VENTANA
        (A, L1, CASTELLO, h(10, 5)),               # sentido contrario: sin horario A->o[9]
    ]
    con.executemany(
        "INSERT INTO pasos_obs(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
        "ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,'salida',?)",
        [(s, l, d, t - 600, t, t, t, 10, t) for s, l, d, t in obs])
    con.commit()
    return {"con": con, "A": A, "B": B, "C": C, "D": D, "E": E, "h": h}


def test_paso_dos_minutos_tarde(net, escenario):
    filas = _crudos(escenario["con"], net, DIA)
    assert len(filas) == 1
    f = filas[0]
    assert (f["estacion_id"], f["hacia_id"]) == (escenario["A"], escenario["B"])
    assert f["retraso_s"] == 120
    assert f["ts_prog"] == escenario["h"](10, 0) and f["ts_paso"] == escenario["h"](10, 2)
    assert f["linea"] == L1 and f["destino"] == BETERA
    assert f["estacion"] == net.estaciones[escenario["A"]].nombre


def test_paso_ambiguo_no_se_devuelve(net, escenario):
    filas = _crudos(escenario["con"], net, DIA)
    assert all(f["estacion_id"] != escenario["C"] for f in filas)
    # y el que queda a 20 min de cualquier hora tampoco
    assert all(f["estacion_id"] != escenario["E"] for f in filas)
    assert abs(30 - 30) < MARGEN_AMBIG and 20 * 60 > VENTANA     # premisas del escenario


def test_paso_deja_de_ser_ambiguo_si_la_otra_hora_se_aleja(net, escenario):
    con = escenario["con"]
    con.execute("UPDATE horario_paso SET ts_prog=?, hora='11:05' WHERE hora='11:01'",
                (escenario["h"](11, 5),))
    con.commit()
    filas = _crudos(con, net, DIA)
    c = [f for f in filas if f["estacion_id"] == escenario["C"]]
    assert len(c) == 1 and c[0]["retraso_s"] == 30


def test_retrasos_igual_que_crudos_sin_incoherentes(net, escenario):
    assert retrasos(escenario["con"], net, DIA) == _crudos(escenario["con"], net, DIA)


def test_otro_dia_sin_datos(net, escenario):
    assert _crudos(escenario["con"], net, "2026-03-11") == []


def test_sentidos_inferidos_vota_con_pasos_vecinos(net, bd):
    """L1 'Benimaclet' (fuera del trazado): 6 pares de pasos por estaciones
    vecinas con 3 min de diferencia en orden creciente -> sentido +1."""
    con = bd.con
    o = _orden(net)
    t0, _ = _rango_dia(DIA)
    filas = []
    for k in range(6):
        t = t0 + 10 * 3600 + k * 1800
        filas += [(o[5], L1, "Benimaclet", t), (o[6], L1, "Benimaclet", t + 180)]
    con.executemany(
        "INSERT INTO pasos_obs(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
        "ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,'salida',?)",
        [(s, l, d, t - 600, t, t, t, 10, t) for s, l, d, t in filas])
    con.commit()
    assert sentidos_inferidos(con, net, DIA) == {(L1, "Benimaclet"): 1}
    assert sentidos_inferidos(con, net, DIA) is pasos._sent_cache[DIA]    # cacheado


# ------------------------------------------------------------- retraso_vivo

def test_retraso_vivo(net, bd):
    con = bd.con
    o = _orden(net)
    dist = {s: d for d, s in net.comerciales[L1].orden}
    A, B = o[10], o[11]
    t0, _ = _rango_dia(DIA)
    ts = t0 + 10 * 3600                       # 10:00 de DIA
    dia = dt.date.fromtimestamp(ts).isoformat()
    assert dia == DIA
    pasos._incoh_cache[dia] = set()           # sin hilo de fondo
    con.executemany("INSERT INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                    " VALUES (?,?,?,?,?)",
                    [(DIA, A, B, ts + 60, "10:01"), (DIA, A, B, ts + 1500, "10:25")])
    con.commit()
    tren = {"linea": L1, "destino": BETERA, "waypoints": [
        {"d": dist[A] - 300, "eta": 0},
        {"d": dist[A], "eta": 120, "nom": net.estaciones[A].nombre},
        {"d": dist[B], "eta": 300, "nom": net.estaciones[B].nombre},
    ]}
    r = retraso_vivo(con, net, tren, ts)
    assert r == {"retraso_s": 60, "estacion": net.estaciones[A].nombre, "hora_prog": "10:01"}
    # sin horario para ese día -> None
    assert retraso_vivo(con, net, tren, ts + 86400) is None


def test_retraso_vivo_ambiguo_es_none(net, bd):
    con = bd.con
    o = _orden(net)
    dist = {s: d for d, s in net.comerciales[L1].orden}
    A, B = o[10], o[11]
    t0, _ = _rango_dia(DIA)
    ts = t0 + 10 * 3600
    pasos._incoh_cache[DIA] = set()
    con.executemany("INSERT INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                    " VALUES (?,?,?,?,?)",
                    [(DIA, A, B, ts + 90, "10:01"), (DIA, A, B, ts + 150, "10:02")])
    con.commit()
    tren = {"linea": L1, "destino": BETERA, "waypoints": [
        {"d": dist[A] - 300, "eta": 0},
        {"d": dist[A], "eta": 120, "nom": net.estaciones[A].nombre}]}
    assert retraso_vivo(con, net, tren, ts) is None


# --------------------------------------------------------- retraso_arribada

def test_retraso_arribada(net, bd):
    """Una llegada anunciada en A hacia Bétera prevista a las 10:02 frente al
    horario de paso A->B de las 10:00: +120 s. Ambigua, lejana o sin horario -> None."""
    con = bd.con
    o = _orden(net)
    A, B = o[10], o[11]
    t0, _ = _rango_dia(DIA)
    h = lambda hh, mm: t0 + hh * 3600 + mm * 60  # noqa: E731
    pasos._incoh_cache[DIA] = set()
    con.executemany("INSERT INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                    " VALUES (?,?,?,?,?)",
                    [(DIA, A, B, h(10, 0), "10:00"), (DIA, A, B, h(10, 30), "10:30"),
                     (DIA, A, B, h(10, 31), "10:31")])
    con.commit()
    assert retraso_arribada(con, net, A, L1, BETERA, h(10, 2)) == {"retraso_s": 120, "hora_prog": "10:00"}
    assert retraso_arribada(con, net, A, L1, BETERA, h(9, 59)) == {"retraso_s": -60, "hora_prog": "10:00"}
    assert retraso_arribada(con, net, A, L1, BETERA, h(10, 30) + 30) is None     # ambigua 10:30/10:31
    assert retraso_arribada(con, net, A, L1, BETERA, h(10, 16)) is None          # a 16 min: fuera de VENTANA
    assert retraso_arribada(con, net, A, L1, CASTELLO, h(10, 2)) is None         # sin horario A->o[9]
    assert retraso_arribada(con, net, A, L1, "Benimaclet", h(10, 2)) is None     # destino fuera del trazado
    assert retraso_arribada(con, net, A, L1, "Benimaclet", h(10, 2), sentido=1) == {"retraso_s": 120, "hora_prog": "10:00"}
    # par incoherente -> None
    pasos._incoh_cache[DIA] = {(A, B)}
    assert retraso_arribada(con, net, A, L1, BETERA, h(10, 2)) is None
    # otro día sin horario -> None
    pasos._incoh_cache["2026-03-11"] = set()
    assert retraso_arribada(con, net, A, L1, BETERA, h(10, 2) + 86400) is None


# ------------------------------------------------------ cancelados_estacion

@pytest.fixture
def escenario_canc(net, bd):
    """A->B con horario 10:00, 10:10, 10:20, 10:30 y pasos reales a las 10:01
    y 10:17 (tarde pero antes del siguiente). Lecturas de la estación cada
    minuto de 09:30 a 11:10. Con HORIZONTE_CANC=1800 el de las 10:20 se juzga
    a partir de las 10:51 y el de las 10:30 (siguiente ficticio 10:40) a
    partir de las 11:01."""
    con = bd.con
    o = _orden(net)
    A, B = o[10], o[11]
    t0, _ = _rango_dia(DIA)
    h = lambda hh, mm: t0 + hh * 3600 + mm * 60  # noqa: E731
    pasos._incoh_cache[DIA] = set()           # sin hilo de fondo
    con.executemany("INSERT INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
                    " VALUES (?,?,?,?,?)",
                    [(DIA, A, B, h(10, m), f"10:{m:02d}") for m in (0, 10, 20, 30)])
    obs = [(A, L1, BETERA, h(10, 1)), (A, L1, BETERA, h(10, 17))]
    con.executemany(
        "INSERT INTO pasos_obs(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
        "ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,'salida',?)",
        [(s, l, d, t - 600, t, t, t, 10, t) for s, l, d, t in obs])
    con.executemany("INSERT INTO snapshots(ts) VALUES (?)",
                    [(h(9, 30) + 60 * k,) for k in range(101)])
    con.commit()

    def paso(t, estado="salida"):
        con.execute(
            "INSERT INTO pasos_obs(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
            "ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (A, L1, BETERA, t - 600, t, t, t, 10, estado, t))
        con.commit()

    def canc(ahora, sid=A):
        return [c["hora"] for c in cancelados_estacion(con, net, sid, DIA, ahora=ahora)]

    return {"con": con, "A": A, "B": B, "h": h, "paso": paso, "canc": canc}


def test_cancelado_el_que_no_paso_ni_antes_del_siguiente(net, escenario_canc):
    e = escenario_canc
    # a las 10:55: el de las 10:10 pasó a las 10:17 (tarde, pero antes de las 10:20):
    # NO cancelado; el de las 10:20 no pasó ni antes de las 10:30 y en la media hora
    # siguiente no pasó ningún tren (déficit): CANCELADO; el de las 10:30: pendiente
    res = cancelados_estacion(e["con"], net, e["A"], DIA, ahora=e["h"](10, 55))
    assert [c["hora"] for c in res] == ["10:20"]
    c = res[0]
    assert set(c) == {"hora", "ts_prog", "hacia_id", "hacia", "lineas"}
    assert c["ts_prog"] == e["h"](10, 20) and c["hacia_id"] == e["B"]
    assert c["hacia"] == net.estaciones[e["B"]].nombre
    assert L1 in c["lineas"]


def test_cancelado_pendiente_hasta_confirmar_el_deficit(net, escenario_canc):
    e, h = escenario_canc, escenario_canc["h"]
    assert e["canc"](h(10, 20) + 30) == []          # aún puede pasar antes de las 10:30
    assert e["canc"](h(10, 31) + 30) == []          # no pasó, pero el horizonte de media hora no ha vencido
    assert e["canc"](h(10, 52)) == ["10:20"]        # vencido (10:50 + 60 s): cancelado
    assert e["canc"](h(11, 5)) == ["10:20", "10:30"]  # el último también (su siguiente ficticio es 10:40)


def test_cancelado_un_paso_real_solo_cuenta_una_vez(net, escenario_canc):
    """Un paso a las 10:19:30 está en la ventana del 10:10 (ya ocupada por el de
    las 10:17) y en la del 10:20: se lo lleva el 10:20 por orden."""
    e, h = escenario_canc, escenario_canc["h"]
    e["paso"](h(10, 19) + 30)
    assert e["canc"](h(10, 55)) == []
    assert e["canc"](h(11, 5)) == ["10:30"]


def test_retraso_mayor_que_el_intervalo_no_es_cancelacion(net, escenario_canc):
    """El de las 10:20 pasa a las 10:31 y el de las 10:30 a las 10:39: ninguno
    cae en su ventana literal, pero en la media hora pasaron tantos trenes
    como estaban programados -> solo retraso, nada cancelado."""
    e, h = escenario_canc, escenario_canc["h"]
    e["paso"](h(10, 31)); e["paso"](h(10, 39))
    assert e["canc"](h(11, 5)) == []


def test_cancelado_sin_cobertura_no_se_juzga(net, escenario_canc):
    e, h = escenario_canc, escenario_canc["h"]
    # sin lecturas de la estación entre 10:09 y 10:41 (poller caído): "sin datos", no cancelado
    e["con"].execute("DELETE FROM snapshots WHERE ts>=? AND ts<?", (h(10, 9), h(10, 41)))
    e["con"].commit()
    assert e["canc"](h(10, 55)) == []
    # las lecturas fallidas de ESA estación tampoco cuentan como cobertura
    e["con"].execute("DELETE FROM snapshots")
    e["con"].executemany("INSERT INTO snapshots(ts) VALUES (?)", [(h(9, 30) + 60 * k,) for k in range(101)])
    e["con"].executemany("INSERT INTO lecturas_fallidas(snapshot_id,ts,estacion_id) VALUES (?,?,?)",
                         [(0, h(9, 30) + 60 * k, e["A"]) for k in range(101)])
    e["con"].commit()
    assert e["canc"](h(10, 55)) == []


def test_cancelado_salvaguardas(net, escenario_canc):
    e, h = escenario_canc, escenario_canc["h"]
    o = _orden(net)
    assert e["canc"](h(10, 55), sid=o[20]) == []              # estación sin horario
    e["paso"](h(10, 21), estado="desaparecida")               # no es un paso real: sigue cancelado
    assert e["canc"](h(10, 55)) == ["10:20"]
    pasos._incoh_cache[DIA] = {(e["A"], e["B"])}              # par con horario incoherente: no se juzga
    assert e["canc"](h(10, 55)) == []
    pasos._incoh_cache[DIA] = set()
    # la fuente no publica esa dirección en esta estación (ningún paso real en el día)
    e["con"].execute("DELETE FROM pasos_obs"); e["con"].commit()
    assert e["canc"](h(11, 5)) == []


# ------------------------------------------------------------- datos reales

def test_crudos_sobre_una_hora_real(net, bd_real, extracto):
    dia = extracto["meta"]["dia"]
    filas = _crudos(bd_real.con, net, dia)
    assert 500 <= len(filas) <= len(extracto["pasos_obs"])
    pares_prog = {(h["estacion_id"], h["hacia_id"]) for h in extracto["horario_paso"]}
    for f in filas:
        assert (f["estacion_id"], f["hacia_id"]) in pares_prog
        assert abs(f["retraso_s"]) <= VENTANA
        assert f["retraso_s"] == f["ts_paso"] - f["ts_prog"]
        assert f["linea"] in range(1, 11)
    assert [f["ts_paso"] for f in filas] == sorted(f["ts_paso"] for f in filas)
    # un martes normal: el retraso mediano de la red está en pocos minutos
    med = statistics.median(f["retraso_s"] for f in filas)
    assert -120 <= med <= 300, med
    res = resumen_lineas(filas)
    assert set(res) <= set(range(1, 11)) and sum(r["n"] for r in res.values()) == len(filas)


def test_retrasos_reales_quitan_solo_pares_incoherentes(net, bd_real, extracto):
    dia = extracto["meta"]["dia"]
    crudos = _crudos(bd_real.con, net, dia)
    malos = pares_incoherentes(crudos, extremos=pasos._extremos(net, crudos))
    finales = retrasos(bd_real.con, net, dia)
    assert len(finales) == len([f for f in crudos if (f["estacion_id"], f["hacia_id"]) not in malos])
    assert all((f["estacion_id"], f["hacia_id"]) not in malos for f in finales)


def test_un_tren_nunca_va_adelantado():
    """Regla del 21/10/2026: la hora programada de un paso es la ÚLTIMA que no
    queda después de él. Un paso 2 min 'antes' de una hora es el tren anterior
    con retraso (o el anterior se canceló), no un tren adelantado."""
    from backend.pasos import _casar_slot, ADELANTO_MAX, VENTANA
    t = 1_700_000_000
    slots = [t, t + 600, t + 1200]
    assert _casar_slot(slots, t + 480) == (t, 480)            # no (t+600, -120)
    assert _casar_slot(slots, t + 600 - ADELANTO_MAX) == (t + 600, -ADELANTO_MAX)  # redondeo tolerado
    assert _casar_slot(slots, t - ADELANTO_MAX - 1) is None   # antes de la primera: nada
    assert _casar_slot(slots, t + 1200 + VENTANA + 1) is None  # demasiado tarde
    assert _casar_slot([t, t + 60, t + 1200], t + 90) is None  # dos horas pegadas: ambiguo

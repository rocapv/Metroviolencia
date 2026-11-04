"""Lectures compactes (un blob per pasada), noms d'URL de les estacions i
recorregut d'un tren que ja no circula."""
from __future__ import annotations

import time

from backend import cabeceras, historia, lectures


def test_lectures_anada_i_tornada(bd):
    files = [(30, 1, "Bétera", 420, "7 min", None), (1, 1, "Bétera", 60, "1 min", 2),
             (1, 1, "Seminari - CEU", 900, "15 min", None)]
    ts = int(time.time())
    lectures.guarda(bd.con, 7, ts, files)
    assert lectures.de_pasada(bd.con, 7) == sorted(files, key=lambda f: (f[0], f[3]))
    assert lectures.de_pasada(bd.con, 8) == []
    assert [s for s, _t, _f in lectures.pasades(bd.con, desde_ts=0)] == [7]


def test_destins_de_capcalera_es_van_sumant(bd):
    # Castelló (id 1) és capçalera de L1: cada pasada suma els destins que hi veu
    ts = int(time.time())
    for k in range(6):
        lectures.guarda(bd.con, 100 + k, ts + k, [(1, 1, "Bétera", 60, "1 min", None),
                                                  (30, 1, "Bétera", 300, "5 min", None)])
    bd.con.commit()
    cab = cabeceras.POR_ID[1]
    assert cabeceras.destinos_de(bd.con, cab) == [("Bétera", 1, 6)]


def test_purga_lectures(bd):
    vell = int(time.time()) - 9 * 86400
    lectures.guarda(bd.con, 1, vell, [(1, 1, "Bétera", 60, "1 min", None)])
    lectures.guarda(bd.con, 2, int(time.time()), [(1, 1, "Bétera", 60, "1 min", None)])
    assert lectures.purga(bd.con, 7, log=lambda *_: None) == 1
    assert [s for s, _t, _f in lectures.pasades(bd.con)] == [2]


def test_slug_estacions(net):
    assert historia.slug("Àngel Guimerà") == "angelguimera"
    assert historia.slug("Seminari - CEU") == "seminariceu"
    assert historia.slug("Col·legi El Vedat") == "collegielvedat"
    s = historia.slugs(net)
    assert len(s) == len(net.estaciones)            # cap repetit
    assert all(k.isalnum() and k == k.lower() for k in s)


def test_historia_tren_desconegut(bd_real, net):
    assert historia.historia(bd_real.con, net, 999999999) is None


def test_compacta_nomes_de_matinada_i_si_sobra_espai(tmp_path):
    import datetime as dt
    import sqlite3

    from backend import purga
    db = tmp_path / "c.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(4000)])
    con.commit()
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    migdia = dt.datetime(2026, 11, 4, 12, 0).timestamp()
    nit = dt.datetime(2026, 11, 4, 3, 30).timestamp()
    log = lambda *_: None
    assert purga.compacta(db, log, ara=migdia) is False      # de dia no
    abans = db.stat().st_size
    assert purga.compacta(db, log, ara=nit) is True          # de nit i amb espai lliure, sí
    assert db.stat().st_size < abans
    assert purga.compacta(db, log, ara=nit) is False         # ja no sobra: no repeteix

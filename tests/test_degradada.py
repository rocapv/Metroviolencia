"""api._pasadas/_degradat: si FGV no contesta, se sirve la última pasada buena (06/11/2026)."""
import sqlite3

from backend.api import MIN_ESTACIONES, _degradat, _pasadas


def _con(oks):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, ts INTEGER, ts_fin INTEGER, estaciones_ok INTEGER)")
    con.execute("CREATE TABLE trenes (snapshot_id INTEGER, track_id INTEGER)")
    for i, ok in enumerate(oks, 1):
        con.execute("INSERT INTO snapshots VALUES (?,?,?,?)", (i, 1000 + 120 * i, 1030 + 120 * i, ok))
        con.execute("INSERT INTO trenes VALUES (?, 1)", (i,))
    return con


def test_pasada_buena_es_la_ultima():
    b, u = _pasadas(_con([144, 140]))
    assert b["snapshot_id"] == u["snapshot_id"] == 2
    assert _degradat(b, u) is None


def test_si_fgv_no_contesta_se_sirve_la_ultima_buena():
    b, u = _pasadas(_con([144, 130, 0, 67]))
    assert b["snapshot_id"] == 2 and u["snapshot_id"] == 4
    d = _degradat(b, u)
    assert d["llegides"] == 67 and d["mostrant"] == b["ts"]


def test_sin_ninguna_buena_avisa_igual():
    b, u = _pasadas(_con([0, MIN_ESTACIONES - 1]))
    assert _degradat(b, u)["llegides"] == MIN_ESTACIONES - 1


def test_pasadas_antiguas_sin_recuento_son_buenas():
    b, u = _pasadas(_con([None, None]))
    assert b["snapshot_id"] == 2 and _degradat(b, u) is None


def test_enllac_escrit_a_ma_va_a_la_forma_normal():
    from fastapi.testclient import TestClient
    from backend.api import app
    c = TestClient(app, headers={"X-Forwarded-For": "192.168.1.50"})   # LAN (auth per IP)
    r = c.get("/Burjassot", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "./burjassot"
    r = c.get("/%C3%80ngel%20Guimer%C3%A0", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "./angelguimera"
    assert c.get("/NoExisteix", follow_redirects=False).status_code == 302
    assert c.get("/style.css").status_code == 200            # els estàtics no es toquen

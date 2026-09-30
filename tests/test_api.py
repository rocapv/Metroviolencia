"""backend/api.py con TestClient: auth por LAN/token y endpoints sobre una BD temporal."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from backend import api, pasos, poller

LAN = {"X-Forwarded-For": "192.168.1.50"}
FUERA = {"X-Forwarded-For": "8.8.8.8"}


@pytest.fixture
def cliente(monkeypatch, tmp_path, bd_real, extracto):
    """TestClient con la BD apuntando al extracto real y sin ficheros de
    estado en data/ (token, cliente activo, ventana abierta)."""
    monkeypatch.setattr(api, "DB", bd_real.path)
    monkeypatch.setattr(poller, "DB", bd_real.path)
    monkeypatch.setattr(api, "CLIENT_FILE", tmp_path / "last_client.txt")
    monkeypatch.setattr(api, "TOKEN_FILE", tmp_path / "token.txt")     # no existe
    monkeypatch.setattr(api, "OPEN_FILE", tmp_path / "open_until.txt")  # no existe
    monkeypatch.delenv("METRO_TOKEN", raising=False)
    monkeypatch.setattr(api, "_net_cache", None)
    monkeypatch.setattr(api, "_ret_cache", {})
    # sin hilo de fondo de paradas incoherentes
    for d in (dt.date.today().isoformat(), extracto["meta"]["dia"]):
        pasos._incoh_cache[d] = set()
    bd_real.con.close()          # la API abre su propia conexión
    with TestClient(app_sin_raise()) as c:
        yield c


def app_sin_raise():
    return api.app


# ------------------------------------------------------------------ auth

def test_health_desde_lan(cliente, extracto):
    r = cliente.get("/api/health", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["snapshots"] == len(extracto["snapshots"])
    assert j["ultimo_ts"] == max(s["ts"] for s in extracto["snapshots"])
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.7", "172.16.4.4", "172.31.255.1", "192.168.0.2"])
def test_rangos_lan_sin_token(cliente, ip):
    assert cliente.get("/api/health", headers={"X-Forwarded-For": ip}).status_code == 200


@pytest.mark.parametrize("ip", ["172.15.0.1", "172.32.0.1", "8.8.8.8", "193.0.0.1"])
def test_fuera_de_lan_sin_token_configurado_es_403(cliente, ip):
    assert cliente.get("/api/health", headers={"X-Forwarded-For": ip}).status_code == 403


def test_testclient_sin_xff_no_es_lan(cliente):
    """request.client.host del TestClient es 'testclient': no es RFC1918."""
    assert cliente.get("/api/health").status_code == 403


def test_ultima_ip_de_xff_es_la_que_cuenta(cliente):
    # el cliente puede inyectar lo que quiera al principio; Apache añade la real al final
    assert cliente.get("/api/health", headers={"X-Forwarded-For": "192.168.1.9, 8.8.8.8"}).status_code == 403
    assert cliente.get("/api/health", headers={"X-Forwarded-For": "8.8.8.8, 192.168.1.9"}).status_code == 200


def test_token_desde_fuera(cliente, monkeypatch, tmp_path):
    tok = tmp_path / "token.txt"
    tok.write_text("secreto-de-prueba\n", encoding="utf-8")
    monkeypatch.setattr(api, "TOKEN_FILE", tok)
    assert cliente.get("/api/health", headers=FUERA).status_code == 403
    assert cliente.get("/api/health?token=malo", headers=FUERA).status_code == 403
    r = cliente.get("/api/health?token=secreto-de-prueba", headers=FUERA)
    assert r.status_code == 200
    assert "mvtoken=secreto-de-prueba" in r.headers.get("set-cookie", "")
    # con la cookie, sin token en la URL
    cliente.cookies.set("mvtoken", "secreto-de-prueba")
    r2 = cliente.get("/api/health", headers=FUERA)
    assert r2.status_code == 200
    assert "set-cookie" not in r2.headers
    cliente.cookies.clear()
    assert cliente.get("/api/health", headers=FUERA).status_code == 403
    # la LAN sigue sin necesitar token
    assert cliente.get("/api/health", headers=LAN).status_code == 200


def test_ventana_abierta_sin_token(cliente, monkeypatch, tmp_path):
    import time
    abierto = tmp_path / "open_until.txt"
    abierto.write_text(str(time.time() + 600), encoding="utf-8")
    monkeypatch.setattr(api, "OPEN_FILE", abierto)
    assert cliente.get("/api/health", headers=FUERA).status_code == 200
    abierto.write_text(str(time.time() - 1), encoding="utf-8")
    assert cliente.get("/api/health", headers=FUERA).status_code == 403


# ------------------------------------------------------------- endpoints

def test_network(cliente):
    r = cliente.get("/api/network", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    assert len(j["estaciones"]) == 144
    assert sorted(int(k) for k in j["comerciales"]) == list(range(1, 11))
    assert isinstance(j["cierres"], list)
    assert "València Sud" in j["estaciones_retorno"]


def test_trains_desde_tabla_trenes(cliente, extracto, tmp_path):
    r = cliente.get("/api/trains", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    guardados = [t for t in extracto["trenes"] if t["snapshot_id"] == 31114]
    assert j["ts"] == guardados[0]["ts"]
    assert j["n"] == len(guardados) == len(j["trenes"])
    assert sorted(t["id"] for t in j["trenes"]) == sorted(t["track_id"] for t in guardados)
    for t in j["trenes"]:
        assert {"id", "linea", "destino", "color", "lat", "lng", "prox_estacion",
                "eta_seg", "origen", "waypoints", "retraso"} <= set(t)
        assert isinstance(t["waypoints"], list) and t["waypoints"][0]["eta"] == 0
        assert t["retraso"] is None or {"retraso_s", "estacion", "hora_prog"} == set(t["retraso"])
        if t["origen"]:
            assert t["retraso"] is None
    # con horario de paso del día hay retrasos en vivo calculados para algunos
    assert sum(t["retraso"] is not None for t in j["trenes"]) > 0
    assert j["noinfo"] == []
    assert j["edad_seg"] > 0 and j["srv_ms"] > 0
    # marcó cliente activo (cadencia rápida del poller)
    assert (tmp_path / "last_client.txt").exists()


def test_trains_fallback_estimando_al_vuelo(cliente, bd_real, extracto):
    import sqlite3
    con = sqlite3.connect(bd_real.path)
    con.execute("DELETE FROM trenes")
    con.commit()
    con.close()
    r = cliente.get("/api/trains", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    ultimo = [t for t in extracto["trenes"] if t["snapshot_id"] == 31114]
    assert j["n"] == len(ultimo)                 # mismo estimador que el poller
    assert all("id" not in t for t in j["trenes"])   # sin identidad persistente
    assert j["ts"] == ultimo[0]["ts"]


def test_arrivals(cliente, extracto):
    r = cliente.get("/api/arrivals", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    ult = [a for a in extracto["arrivals"] if a["snapshot_id"] == 31114]
    assert j["ts"] == ult[0]["ts"]
    assert len(j["estaciones"]) == len({a["estacion_id"] for a in ult})
    assert sum(len(v) for v in j["estaciones"].values()) == len(ult)
    for lst in j["estaciones"].values():
        etas = [x["eta_seg"] for x in lst]
        assert etas == sorted(etas)


def test_retards_ruta_dia_real(cliente, extracto):
    dia = extracto["meta"]["dia"]
    r = cliente.get(f"/api/retards_ruta?dia={dia}", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    assert j["dia"] == dia and j["n_pasos"] >= 500
    assert set(j["dia_completo"]) <= {str(l) for l in range(1, 11)}
    assert sum(v["n"] for v in j["dia_completo"].values()) == j["n_pasos"]
    assert j["ultima_hora"] == {}          # el extracto es de hace días
    assert isinstance(j["tarde_recientes"], list)
    # cacheado: un día cerrado no se recalcula
    assert cliente.get(f"/api/retards_ruta?dia={dia}", headers=LAN).json() == j


def test_retards_ruta_y_registro_dia_invalido(cliente):
    assert cliente.get("/api/retards_ruta?dia=ayer", headers=LAN).status_code == 400
    assert cliente.get("/api/registro?dia=2026-13-40", headers=LAN).status_code == 400


def test_registro_sin_horario_programado(cliente, extracto):
    dia = extracto["meta"]["dia"]
    r = cliente.get(f"/api/registro?dia={dia}", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    assert j["dia"] == dia
    assert set(j["tipo_dia"]) == {"metro", "tranvia", "l10"}
    assert j["salidas"] == [] and j["resumen"]["por_estado"] == {}


def test_ruta_parametros_invalidos(cliente):
    assert cliente.get("/api/ruta?origen=25&destino=25", headers=LAN).status_code == 400
    assert cliente.get("/api/ruta?origen=25&destino=999999", headers=LAN).status_code == 400
    assert cliente.get("/api/ruta?origen=25", headers=LAN).status_code == 422


def test_ruta_planificador_caido_da_502(cliente, monkeypatch):
    from backend import rutas

    def _rompe(*a, **k):
        raise RuntimeError("sin red")
    monkeypatch.setattr(rutas, "consultar_ruta", _rompe)
    monkeypatch.setattr(poller, "make_session", lambda: object())
    r = cliente.get("/api/ruta?origen=107&destino=54", headers=LAN)
    assert r.status_code == 502
    assert "planificador" in r.json()["error"]


def test_frontend_estatico(cliente):
    r = cliente.get("/", headers=LAN)
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert "no-store" in r.headers["cache-control"]
    assert cliente.get("/", headers=FUERA).status_code == 403

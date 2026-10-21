"""backend/api.py con TestClient: auth por LAN/token y endpoints sobre una BD temporal."""
from __future__ import annotations

import datetime as dt
import threading
import time

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


def test_cookie_del_token_segun_donde_cuelga_la_app(cliente, monkeypatch, tmp_path):
    tok = tmp_path / "token.txt"
    tok.write_text("secreto-de-prueba\n", encoding="utf-8")
    monkeypatch.setattr(api, "TOKEN_FILE", tok)
    from http.cookies import SimpleCookie

    def cookie(headers):
        c = SimpleCookie()
        c.load(cliente.get("/api/health?token=secreto-de-prueba", headers=headers).headers["set-cookie"])
        return c["mvtoken"]

    # ruta heredada (/metroviolencia/ en home.pr1.es:5580): cookie acotada a ese prefijo
    heredada = cookie(FUERA)
    assert heredada["path"] == "/metroviolencia" and not heredada["secure"]
    # vhost dedicado metroviolencia.pr1.es: cuelga de la raíz y va por HTTPS
    dedicado = cookie({**FUERA, "X-Forwarded-Prefix": "/", "X-Forwarded-Proto": "https"})
    assert dedicado["path"] == "/" and dedicado["secure"]


def test_acceso_abierto_siempre_sin_token(cliente, monkeypatch, tmp_path):
    abierto = tmp_path / "open_until.txt"
    monkeypatch.setattr(api, "OPEN_FILE", abierto)
    assert cliente.get("/api/health", headers=FUERA).status_code == 403   # sin fichero: cerrado
    abierto.write_text("siempre\n", encoding="utf-8")
    assert cliente.get("/api/health", headers=FUERA).status_code == 200
    abierto.write_text("basura", encoding="utf-8")                        # ilegible: cerrado
    assert cliente.get("/api/health", headers=FUERA).status_code == 403


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
    assert sorted(int(k) for k, c in j["comerciales"].items() if c["variante_de"] is None) == list(range(1, 11))
    assert {int(k) for k, c in j["comerciales"].items() if c["variante_de"] == 4} == {41, 42}
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


def test_ultimo_marca_la_ultima_pasada_servida(cliente, extracto):
    """/api/ultimo: ts de la última pasada con trenes (barato, el cliente lo
    consulta cada 2 s para refrescar justo cuando hay datos nuevos)."""
    r = cliente.get("/api/ultimo", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    ultimo = [t for t in extracto["trenes"] if t["snapshot_id"] == 31114]
    assert j["ts"] == ultimo[0]["ts"]
    assert "ts_fin" in j and "srv_ms" in j
    # /api/trains lleva la misma marca
    assert cliente.get("/api/trains", headers=LAN).json()["ts_fin"] == j["ts_fin"]


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
    # mismo estimador que el poller; desde los ramales de L4 (21/10/2026) el
    # extracto del 29/09 da un tren L4 más (el que espera en Fira València)
    assert j["n"] == len(ultimo) + 1
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
        for x in lst:
            assert "retraso" in x
            assert x["retraso"] is None or set(x["retraso"]) == {"retraso_s", "hora_prog"}
            if x["eta_seg"] >= 3600:
                assert x["retraso"] is None          # "1 h" no tiene minutos
            if x["retraso"] is not None:
                assert abs(x["retraso"]["retraso_s"]) <= pasos.VENTANA
    # con horario de paso del día hay desfases calculados para bastantes llegadas
    con_ret = sum(x["retraso"] is not None for lst in j["estaciones"].values() for x in lst)
    assert con_ret > 100, con_ret


def test_cancelats(cliente, extracto, monkeypatch):
    monkeypatch.setattr(api, "_canc_cache", {})
    dia = extracto["meta"]["dia"]
    r = cliente.get(f"/api/cancelats?estacio=67&dia={dia}", headers=LAN)
    assert r.status_code == 200
    j = r.json()
    assert j["dia"] == dia and j["estacio"] == 67 and isinstance(j["cancelats"], list)
    for c in j["cancelats"]:
        assert set(c) == {"hora", "ts_prog", "hacia_id", "hacia", "lineas"}
        assert isinstance(c["lineas"], list)
    # sin día: hoy (sin horario cargado en la BD de prueba -> lista vacía, pero 200)
    r = cliente.get("/api/cancelats?estacio=67", headers=LAN)
    assert r.status_code == 200 and r.json()["estacio"] == 67 and r.json()["cancelats"] == []
    assert r.json()["dia"] == dt.date.today().isoformat()
    # cacheado 60 s por (estación, día)
    assert cliente.get(f"/api/cancelats?estacio=67&dia={dia}", headers=LAN).json() == j
    assert cliente.get("/api/cancelats?estacio=999999", headers=LAN).status_code == 400
    assert cliente.get("/api/cancelats?estacio=67&dia=ahir", headers=LAN).status_code == 400
    assert cliente.get("/api/cancelats", headers=LAN).status_code == 422
    assert cliente.get("/api/cancelats?estacio=67", headers=FUERA).status_code == 403


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


def test_ruta_no_mezcla_clientes(cliente, monkeypatch):
    """Cuatro clientes piden rutas distintas A LA VEZ: cada uno recibe la suya.

    Los campos `origen`/`destino` de la respuesta los copia la API de los
    parametros, asi que comprobarlos no demuestra nada; esto mira el CONTENIDO
    (el primer paso de la alternativa). Guarda contra meter en el servidor
    cualquier "ultima ruta" global, que serviria la de otro cliente.
    """
    from backend import rutas

    def _planifica(_s, _h, _v, origen, destino):
        time.sleep(0.05)                     # solapa las peticiones entre si
        return {"dia": dt.date.today().isoformat(), "ventana": "2h",
                "alternativas": [{"transbordos": 0, "duracion_min": 10,
                                  "pasos": [{"de": f"EST{origen}", "a": f"EST{destino}",
                                             "transbordo": False, "salidas": [],
                                             "destinos": []}]}]}

    monkeypatch.setattr(rutas, "consultar_ruta", _planifica)
    monkeypatch.setattr(poller, "make_session", lambda: object())

    parejas = [(107, 54), (25, 64), (1, 40), (30, 5)]
    res: dict[tuple[int, int], dict] = {}

    def pide(o, d):
        res[(o, d)] = cliente.get(f"/api/ruta?origen={o}&destino={d}", headers=LAN).json()

    hilos = [threading.Thread(target=pide, args=par) for par in parejas]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    for o, d in parejas:
        paso = res[(o, d)]["alternativas"][0]["pasos"][0]
        assert (paso["de"], paso["a"]) == (f"EST{o}", f"EST{d}"), \
            f"el cliente {o}->{d} ha recibido la ruta {paso['de']}->{paso['a']}"


def test_frontend_estatico(cliente):
    r = cliente.get("/", headers=LAN)
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert "no-store" in r.headers["cache-control"]
    assert cliente.get("/", headers=FUERA).status_code == 403


# ------------------------------------------------------------------ xat

@pytest.fixture
def xat_net(monkeypatch):
    from backend import xat
    monkeypatch.setattr(xat, "_ultims", {})      # tasa por IP limpia en cada test
    return xat


def test_xat_publica_desde_estacion_y_lo_sirve(cliente, xat_net):
    r = cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "alias": "  Roca ",
                                       "text": "  Hola   des de <b>Benimaclet</b>  "}, headers=LAN)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["tipus"] == "estacio" and m["lloc"] == 67 and m["nom_lloc"] == "Benimaclet"
    assert m["alias"] == "Roca" and m["text"] == "Hola des de <b>Benimaclet</b>"   # espacios plegados, sin tocar el texto
    j = cliente.get("/api/xat?tipus=estacio&lloc=67", headers=LAN).json()
    assert [x["id"] for x in j["missatges"]] == [m["id"]]
    feed = cliente.get("/api/xat?des_de=0", headers=LAN).json()
    assert feed["ultim_id"] == m["id"] and feed["missatges"][-1]["id"] == m["id"]
    assert cliente.get("/api/xat?des_de=%d" % m["id"], headers=LAN).json()["missatges"] == []
    assert cliente.get("/api/ultimo", headers=LAN).json()["xat_id"] == m["id"]


def test_xat_desde_tren_vivo_y_tren_muerto(cliente, extracto, xat_net):
    vivo = [t for t in extracto["trenes"] if t["snapshot_id"] == 31114][0]
    r = cliente.post("/api/xat", json={"tipus": "tren", "lloc": vivo["track_id"], "text": "vaig ací"},
                     headers=LAN)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["linea"] == vivo["linea"] and m["desti"] == vivo["destino"]
    assert m["nom_lloc"] == "L%d → %s" % (vivo["linea"], vivo["destino"])
    xat_net._ultims.clear()
    r = cliente.post("/api/xat", json={"tipus": "tren", "lloc": 999999999, "text": "fantasma"}, headers=LAN)
    assert r.status_code == 400 and "circulaci" in r.json()["error"]


@pytest.mark.parametrize("body", [
    {"tipus": "estacio", "lloc": 67, "text": "   "},            # vacío
    {"tipus": "estacio", "lloc": 123456, "text": "hola"},       # estación desconocida
    {"tipus": "cotxe", "lloc": 1, "text": "hola"},              # tipo inválido
    {"tipus": "estacio", "lloc": "x", "text": "hola"},          # lloc no numérico
])
def test_xat_rechaza_mensajes_invalidos(cliente, xat_net, body):
    assert cliente.post("/api/xat", json=body, headers=LAN).status_code == 400


def test_xat_limita_la_tasa_por_ip(cliente, xat_net):
    assert cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "text": "u"}, headers=LAN).status_code == 200
    r = cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "text": "dos"}, headers=LAN)
    assert r.status_code == 429 and "espera" in r.json()["error"]
    # otra IP no está limitada
    assert cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "text": "tres"},
                        headers={"X-Forwarded-For": "192.168.1.51"}).status_code == 200


def test_xat_recorta_texto_y_alias(cliente, xat_net):
    r = cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "alias": "a" * 50, "text": "x" * 500},
                     headers=LAN)
    assert r.status_code == 200
    assert len(r.json()["text"]) == 200 and len(r.json()["alias"]) == 24


def test_xat_fuera_de_la_lan_sin_token_es_403(cliente, xat_net):
    assert cliente.post("/api/xat", json={"tipus": "estacio", "lloc": 67, "text": "hola"}, headers=FUERA).status_code == 403

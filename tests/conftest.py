"""
Fixtures compartidas de la batería de tests de Metroviolencia.

Todo pasa OFFLINE: la red viene de data/network.json (versionado) y lo que en
producción llega por HTTP o de la BD de la Raspa está congelado en
tests/fixtures/ (HTML real de info-estacion, dos respuestas del planificador
y una hora de la BD real de un día laborable).
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))          # para `import backend`

FIXTURES = Path(__file__).resolve().parent / "fixtures"

from backend import horarios, pasos, rutas, salidas  # noqa: E402
from backend.network import load_network  # noqa: E402


# ------------------------------------------------------------------ red

@pytest.fixture(scope="session")
def net():
    """Red cargada de data/network.json (+ tram_tracks.json)."""
    return load_network()


# ------------------------------------------------------------- fixtures fichero

@pytest.fixture(scope="session")
def html_est25() -> str:
    """Fragmento HTML real de info-estacion&id=25 (Àngel Guimerà) con 8 llegadas."""
    return (FIXTURES / "info_estacion_25.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ruta_directa() -> dict:
    """Respuesta real del planificador: Torrent Avinguda (107) -> Llíria (54), L2 directa."""
    return json.loads((FIXTURES / "ruta_directa_107_54.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def ruta_transbordo() -> dict:
    """Respuesta real del planificador: Torrent Avinguda (107) -> Rafelbunyol (64), 1 transbordo."""
    return json.loads((FIXTURES / "ruta_transbordo_107_64.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def extracto() -> dict:
    """Una hora de la BD real de la Raspa (martes 2026-09-29, 10:00-11:00 CEST):
    horario_paso, pasos_obs, snapshots, arrivals y trenes (ver `meta`)."""
    with gzip.open(FIXTURES / "raspa_2026-09-29.json.gz", "rb") as g:
        return json.loads(g.read().decode("utf-8"))


# ------------------------------------------------------------------ BD temporal

@dataclass
class BD:
    path: Path
    con: sqlite3.Connection


def _esquema_completo(con: sqlite3.Connection) -> None:
    """Todas las tablas del poller (snapshots/arrivals/trenes/noinfo/lecturas)
    más las de horarios, salidas y pasos, como hace poller.init_db()."""
    from backend import poller
    con.executescript(poller.SCHEMA)
    horarios.init_db(con)
    salidas.init_db(con)
    pasos.init_db(con)
    con.commit()


@pytest.fixture
def bd(tmp_path) -> BD:
    """SQLite temporal vacía con el esquema completo."""
    path = tmp_path / "metro_test.db"
    con = sqlite3.connect(path)
    _esquema_completo(con)
    yield BD(path, con)
    con.close()


@pytest.fixture
def bd_real(bd: BD, extracto: dict, monkeypatch) -> BD:
    """BD temporal cargada con el extracto real. Fija `_rango_dia` del día del
    extracto a los epoch guardados: el resultado no depende de la zona horaria
    de la máquina que ejecuta los tests."""
    con = bd.con
    con.executemany("INSERT INTO snapshots(id, ts) VALUES (?,?)",
                    [(s["id"], s["ts"]) for s in extracto["snapshots"]])
    con.executemany(
        "INSERT INTO arrivals(snapshot_id,ts,estacion_id,linea,destino,eta_seg,eta_txt)"
        " VALUES (?,?,?,?,?,?,?)",
        [(a["snapshot_id"], a["ts"], a["estacion_id"], a["linea"], a["destino"],
          a["eta_seg"], a["eta_txt"]) for a in extracto["arrivals"]])
    con.executemany(
        "INSERT INTO trenes(snapshot_id,ts,track_id,linea,destino,color,lat,lng,"
        "prox_estacion,eta_seg,origen,d,waypoints) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(t["snapshot_id"], t["ts"], t["track_id"], t["linea"], t["destino"], t["color"],
          t["lat"], t["lng"], t["prox_estacion"], t["eta_seg"], t["origen"], t["d"],
          t["waypoints"]) for t in extracto["trenes"]])
    dia = extracto["meta"]["dia"]
    con.executemany(
        "INSERT OR IGNORE INTO horario_paso(dia,estacion_id,hacia_id,ts_prog,hora)"
        " VALUES (?,?,?,?,?)",
        [(dia, h["estacion_id"], h["hacia_id"], h["ts_prog"], h["hora"])
         for h in extracto["horario_paso"]])
    con.executemany(
        "INSERT INTO pasos_obs(origen_id,linea,destino,ts_primera,ts_ultima,pred_inicial,"
        "ts_pred,n_vistas,estado,ts_salida) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(p["origen_id"], p["linea"], p["destino"], p["ts_primera"], p["ts_ultima"],
          p["pred_inicial"], p["ts_pred"], p["n_vistas"], p["estado"], p["ts_salida"])
         for p in extracto["pasos_obs"]])
    con.commit()

    t0, t1 = extracto["meta"]["t0"], extracto["meta"]["t1"]
    original = salidas._rango_dia

    def _rango_fijo(d: str):
        return (t0, t1) if d == dia else original(d)

    monkeypatch.setattr(pasos, "_rango_dia", _rango_fijo)
    return bd


# ------------------------------------------------------------------ sin red

def _es_loopback(direccion) -> bool:
    host = direccion[0] if isinstance(direccion, tuple) else str(direccion)
    return host in ("localhost", "::1") or host.startswith("127.")


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    """Ningún test sale a la red: conectar un socket a algo que no sea loopback
    falla en el acto. Loopback se permite porque en Windows `socket.socketpair()`
    (bucle asyncio del TestClient, que habla ASGI en memoria) se emula con un
    connect a 127.0.0.1."""
    import socket
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _connect(self, direccion, *a, **k):
        if _es_loopback(direccion):
            return original_connect(self, direccion, *a, **k)
        raise RuntimeError(f"el test intentó salir a la red: {direccion!r}")

    def _connect_ex(self, direccion, *a, **k):
        if _es_loopback(direccion):
            return original_connect_ex(self, direccion, *a, **k)
        raise RuntimeError(f"el test intentó salir a la red: {direccion!r}")

    def _create_connection(direccion, *a, **k):
        raise RuntimeError(f"el test intentó salir a la red: {direccion!r}")

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    monkeypatch.setattr(socket, "create_connection", _create_connection)


# ------------------------------------------------------------ cachés de módulo

@pytest.fixture(autouse=True)
def _cachés_limpias(monkeypatch):
    """Los módulos guardan cachés a nivel de módulo (por día, por consulta...).
    Cada test arranca con ellas vacías para no heredar nada del anterior."""
    monkeypatch.setattr(pasos, "_sent_cache", {})
    monkeypatch.setattr(pasos, "_incoh_cache", {})
    monkeypatch.setattr(pasos, "_incoh_pend", set())
    monkeypatch.setattr(pasos, "_prog_cache", {})
    monkeypatch.setattr(rutas, "_cache", {})
    monkeypatch.setattr(rutas, "_token", None)

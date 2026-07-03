"""
API de Metroviolencia (FastAPI).

Endpoints:
    GET /api/network   -> geometría estática (líneas, estaciones, colores)
    GET /api/trains    -> trenes estimados del último snapshot
    GET /api/arrivals  -> llegadas crudas del último snapshot (por estación)
    GET /api/health    -> estado

Sirve además el frontend estático de ../web en la raíz.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .network import load_network
from .positions import estimar

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
WEB = ROOT / "web"

app = FastAPI(title="Metroviolencia", version="0.1")


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _last_snapshot(con) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()


@app.get("/api/network")
def network():
    return load_network().to_geojson_meta()


@app.get("/api/arrivals")
def arrivals():
    con = _con()
    snap = _last_snapshot(con)
    if not snap:
        return {"ts": None, "estaciones": {}}
    rows = con.execute(
        "SELECT estacion_id,linea,destino,eta_seg,eta_txt FROM arrivals "
        "WHERE snapshot_id=? ORDER BY estacion_id,eta_seg",
        (snap["id"],),
    ).fetchall()
    por_est: dict[int, list] = {}
    for r in rows:
        por_est.setdefault(r["estacion_id"], []).append(
            {"linea": r["linea"], "destino": r["destino"],
             "eta_seg": r["eta_seg"], "eta_txt": r["eta_txt"]}
        )
    return {"ts": snap["ts"], "edad_seg": int(time.time()) - snap["ts"],
            "estaciones": por_est}


@app.get("/api/trains")
def trains():
    con = _con()
    snap = _last_snapshot(con)
    if not snap:
        return {"ts": None, "trenes": []}
    rows = con.execute(
        "SELECT estacion_id,linea,destino,eta_seg FROM arrivals WHERE snapshot_id=?",
        (snap["id"],),
    ).fetchall()
    llegadas = [(r["estacion_id"], r["linea"], r["destino"], r["eta_seg"]) for r in rows]
    net = load_network()
    trenes = [t.to_dict() for t in estimar(net, llegadas)]
    try:
        noinfo = [r["linea"] for r in con.execute(
            "SELECT linea FROM noinfo WHERE snapshot_id=? ORDER BY linea", (snap["id"],)
        ).fetchall()]
    except sqlite3.OperationalError:
        noinfo = []  # tabla aún no creada
    return {"ts": snap["ts"], "edad_seg": int(time.time()) - snap["ts"],
            "n": len(trenes), "trenes": trenes, "noinfo": noinfo}


@app.get("/api/health")
def health():
    con = _con()
    snap = _last_snapshot(con)
    n = con.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
    return {"ok": True, "snapshots": n,
            "ultimo_ts": snap["ts"] if snap else None}


if WEB.exists():
    app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")

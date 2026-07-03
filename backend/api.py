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

import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .network import load_network
from .positions import estimar

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "metro.db"
WEB = ROOT / "web"
TOKEN_FILE = ROOT / "data" / "token.txt"

app = FastAPI(title="Metroviolencia", version="0.1")


def _token() -> str:
    """Token de acceso (hardcodeado en la APK). Si no hay, acceso abierto (dev)."""
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("METRO_TOKEN", "").strip()


def _is_lan(ip: str) -> bool:
    """Rangos de red local (RFC1918) + loopback -> sin token."""
    return (
        ip in ("127.0.0.1", "::1", "localhost")
        or ip.startswith("192.168.")
        or ip.startswith("10.")
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )


def _client_ip(request: Request) -> str:
    # Apache (mod_proxy) AÑADE la IP real al final de X-Forwarded-For; el último
    # valor es el que vio Apache = cliente real (no falsificable por el cliente).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


@app.middleware("http")
async def auth_token(request: Request, call_next):
    """En RED LOCAL no hace falta token. Desde FUERA de la LAN el token es el
    secreto obligatorio: sin él, aun con IP/puerto, la web no responde (403).
    La APK abre .../?token=XXX una vez; se fija la cookie y el resto de
    peticiones (app.js, /api/...) van autenticadas."""
    q = request.query_params.get("token")
    if _is_lan(_client_ip(request)):
        resp = await call_next(request)   # LAN: acceso libre
    else:
        token = _token()
        ok = bool(token) and (q == token or request.cookies.get("mvtoken") == token)
        if not ok:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        resp = await call_next(request)
        if q == token:   # primer acceso externo con token en la URL: fija cookie
            resp.set_cookie("mvtoken", token, max_age=31536000,
                            path="/metroviolencia", httponly=True, samesite="lax")
    # nunca cachear: ni los datos en vivo ni el frontend (evita snapshots viejos
    # y que el WebView sirva un app.js antiguo). Robusto ante root_path del proxy.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


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

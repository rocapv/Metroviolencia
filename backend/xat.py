"""
Xat de la red: mensajes cortos escritos "desde" un tren (el que lleva el
usuario, elegido por cercanía en su propio móvil) o "desde" una estación, que
leen todos los usuarios del mapa.

- Un mensaje = (tipus 'tren'|'estacio', lloc = track_id o id de estación,
  alias opcional, text). Del tren se guardan línea, destino y rótulo para que
  el mensaje siga legible cuando el tren haya acabado su recorrido.
- Sin cuentas: el alias es libre. Límite de longitud, tasa por IP y retención
  corta (RETENER_DIES) son todo el control. Fuera de la LAN ya hace falta el
  token de acceso para escribir (middleware de api.py).
- El cliente no envía su posición nunca: la cercanía al tren se calcula en el
  propio dispositivo; aquí solo llega el track_id elegido.
"""
from __future__ import annotations

import re
import sqlite3
import time

MAX_TEXT = 200
MAX_ALIAS = 24
MIN_SEPARACIO = 8          # s entre mensajes de una misma IP
MAX_PER_HORA = 40          # mensajes por IP y hora
RETENER_DIES = 7           # días que se guardan los mensajes
MAX_FEED = 100             # máximo por petición al feed

SCHEMA = """
CREATE TABLE IF NOT EXISTS xat (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    tipus    TEXT    NOT NULL,     -- 'tren' | 'estacio'
    lloc     INTEGER NOT NULL,     -- track_id del tren | id de la estación
    linea    INTEGER,              -- tren: su línea
    desti    TEXT,                 -- tren: su destino
    nom_lloc TEXT    NOT NULL,     -- rótulo legible: "L1 → Bétera" | "Benimaclet"
    alias    TEXT    NOT NULL DEFAULT '',
    text     TEXT    NOT NULL,
    ip       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_xat_lloc ON xat(tipus, lloc, id);
CREATE INDEX IF NOT EXISTS idx_xat_ts ON xat(ts);
"""

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ultims: dict[str, list[int]] = {}      # ip -> ts de sus últimos mensajes (1 h)


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def neteja(s: str, maxim: int) -> str:
    s = _CONTROL.sub("", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxim]


def _tasa_ok(ip: str, ara: int) -> tuple[bool, str]:
    lst = [t for t in _ultims.get(ip, []) if ara - t < 3600]
    _ultims[ip] = lst
    if lst and ara - lst[-1] < MIN_SEPARACIO:
        return False, f"espera {MIN_SEPARACIO - (ara - lst[-1])} s"
    if len(lst) >= MAX_PER_HORA:
        return False, "massa missatges en una hora"
    return True, ""


def _fila(r) -> dict:
    return {"id": r["id"], "ts": r["ts"], "tipus": r["tipus"], "lloc": r["lloc"],
            "linea": r["linea"], "desti": r["desti"], "nom_lloc": r["nom_lloc"],
            "alias": r["alias"], "text": r["text"]}


def publicar(con: sqlite3.Connection, net, ip: str, tipus: str, lloc, alias: str,
             text: str) -> tuple[dict | None, str]:
    """Valida y guarda un mensaje. Devuelve (mensaje, error)."""
    init_db(con)
    text = neteja(text, MAX_TEXT)
    alias = neteja(alias, MAX_ALIAS)
    if not text:
        return None, "missatge buit"
    try:
        lloc = int(lloc)
    except (TypeError, ValueError):
        return None, "lloc invàlid"
    linea = desti = None
    if tipus == "estacio":
        e = net.estaciones.get(lloc)
        if e is None:
            return None, "estació desconeguda"
        nom = e.nombre
    elif tipus == "tren":
        # el tren tiene que existir en la última pasada (track_id vivo)
        r = con.execute(
            "SELECT linea, destino FROM trenes WHERE track_id=? AND snapshot_id="
            "(SELECT MAX(snapshot_id) FROM trenes)", (lloc,)).fetchone()
        if r is None:
            return None, "eixe tren ja no està en circulació"
        linea, desti = r[0], r[1]
        nom = f"L{linea} → {desti}"
    else:
        return None, "tipus invàlid"
    ara = int(time.time())
    ok, motiu = _tasa_ok(ip, ara)
    if not ok:
        return None, motiu
    cur = con.execute(
        "INSERT INTO xat(ts,tipus,lloc,linea,desti,nom_lloc,alias,text,ip) VALUES (?,?,?,?,?,?,?,?,?)",
        (ara, tipus, lloc, linea, desti, nom, alias, text, ip))
    con.commit()
    _ultims.setdefault(ip, []).append(ara)
    r = con.execute("SELECT * FROM xat WHERE id=?", (cur.lastrowid,)).fetchone()
    return _fila(r), ""


def del_lloc(con: sqlite3.Connection, tipus: str, lloc: int, n: int = 20) -> list[dict]:
    """Últimos n mensajes de un tren o estación, del más antiguo al más nuevo."""
    init_db(con)
    rows = con.execute("SELECT * FROM xat WHERE tipus=? AND lloc=? ORDER BY id DESC LIMIT ?",
                       (tipus, int(lloc), n)).fetchall()
    return [_fila(r) for r in reversed(rows)]


def des_de(con: sqlite3.Connection, des_de_id: int, n: int = MAX_FEED) -> list[dict]:
    """Mensajes con id > des_de_id (feed en vivo); si des_de_id es 0, los últimos n."""
    init_db(con)
    if des_de_id <= 0:
        rows = con.execute("SELECT * FROM xat ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [_fila(r) for r in reversed(rows)]
    rows = con.execute("SELECT * FROM xat WHERE id>? ORDER BY id LIMIT ?",
                       (des_de_id, n)).fetchall()
    return [_fila(r) for r in rows]


def ultim_id(con: sqlite3.Connection) -> int:
    try:
        r = con.execute("SELECT MAX(id) FROM xat").fetchone()
        return int(r[0] or 0)
    except sqlite3.OperationalError:
        return 0


def purgar(con: sqlite3.Connection) -> int:
    init_db(con)
    cur = con.execute("DELETE FROM xat WHERE ts<?", (int(time.time()) - RETENER_DIES * 86400,))
    con.commit()
    return cur.rowcount

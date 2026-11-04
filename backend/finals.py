"""
Com va acabar un tren que deixa de veure's a mitjan trajecte (historia: 'perdut').

Fins al 04/11/2026 la web hi posava una marca roja «Desapareix» i prou. Mirant
un dia sencer (03/11): de 3.866 trens perduts a mitjan trajecte, 3.746 els van
veure passar les estacions següents a l'hora que tocava. El tren seguia: el
que es perdia era el nostre seguiment, no el tren. Ara cada perdut té un
veredicte, i només és «cancel·lat» quan ho diuen les dades:

    continua     les estacions per davant van registrar el pas de la seua línia
                 a l'hora prevista (pasos_obs, que ve dels panells i no depén del
                 seguiment). Si es troba, `seguit_per` = l'id amb què va seguir.
    cancellat    a les estacions per davant NO hi va passar i el seu horari hi té
                 un tren que falta (pasos.cancelados_estacion: hora programada
                 sense pas i amb dèficit de trens confirmat).
    retirat      per davant ja no hi havia cap tren programat en eixe sentit
                 (fi de servei o tren que es retira a cotxeres).
    sense_dades  el poller no va llegir res durant una estona just després
                 (HUECO_DADES): no se sap què va passar.
    pendent      encara no ha passat prou temps per a confirmar una cancel·lació
                 (pasos.HORIZONTE_CANC després del pas previst).
    indeterminat cap de les anteriors (o FGV no en diu el destí).

Els veredictes definitius es guarden en `finals_tren` (es purguen amb `trenes`).

    python -m backend.finals [--dia YYYY-MM-DD]     # recompte del dia
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time

from . import pasos
from .salidas import HORIZONTE, HUECO_MAX, _cobertura, _continuo, _rango_dia
from .tracking import d_previst, llega_a_destino

HUECO_DADES = 660     # s: més que açò sense cap pasada just després de perdre'l -> sense_dades
MIN_ETA = 60          # s: l'estació a la qual arribava ja (eta < 1 min) no compta: potser hi era
MAX_EST = 5           # estacions per davant que es miren
VIST_ABANS = 180      # s: un pas de la seua línia fins a 3 min abans del previst...
VIST_DESPRES = 420    # s: ... i fins a 7 min després és ell
CANC_FINESTRA = 900   # s: hora programada que falta a menys de 15 min del pas previst
SEGUIT_S = 1800       # s: l'id que el continua ha d'aparéixer en 30 min
SEGUIT_M = 3000       # m: ... i a menys d'açò d'on havia d'estar

DEFINITIUS = ("continua", "cancellat", "retirat", "sense_dades", "indeterminat")

DDL = """
CREATE TABLE IF NOT EXISTS finals_tren (
    track_id  INTEGER PRIMARY KEY,
    ts_fi     INTEGER NOT NULL,
    linea     INTEGER,
    destino   TEXT,
    veredicte TEXT    NOT NULL,
    detall    TEXT    NOT NULL           -- JSON (el que torna `veredicte`)
);
CREATE INDEX IF NOT EXISTS idx_finals_ts ON finals_tren(ts_fi);
"""


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(DDL)


def _hhmm(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


class Context:
    """El que val per a tots els trens d'un dia (horari, sentits, cancel·lats
    per estació): calcular-ho una vegada quan es mira un dia sencer."""

    def __init__(self, con, net, dia: str, ahora: int):
        self.con, self.net, self.dia, self.ahora = con, net, dia, ahora
        self.prog = pasos._prog_dia(con, dia)
        self.dudosos = pasos.incoherentes(con, net, dia) if self.prog else set()
        self.sent = pasos.sentidos_inferidos(con, net, dia) if self.prog else {}
        self._canc: dict[int, list] = {}
        self.vias = {k: (lc.variante_de, lc.d_union)
                     for k, lc in net.comerciales.items() if lc.variante_de}

    def cancelats(self, sid: int) -> list[dict]:
        if sid not in self._canc:
            self._canc[sid] = pasos.cancelados_estacion(
                self.con, self.net, sid, self.dia, ahora=self.ahora, dudosos=self.dudosos)
        return self._canc[sid]


def _seguit_per(con, net, ctx: Context, track_id: int, snap_fi: int, ts_fi: int,
                linea: int, via: int, wps: list) -> int | None:
    """L'id nou que continua el tren: el primer que naix (fora de capçalera) en
    SEGUIT_S, de la mateixa línia, en el mateix sentit i prop d'on havia
    d'estar segons les ETA de la seua última pasada."""
    from .tracking import _vies_compatibles
    d0 = wps[0]["d"]
    dir_ = 1 if wps[-1]["d"] >= d0 else -1
    # fita superior per id de pasada: amb només `ts<=` l'índex per snapshot_id
    # recorria tota la taula des de la pasada on es va perdre
    snap_max = con.execute("SELECT MAX(id) FROM snapshots WHERE ts<=?",
                           (ts_fi + SEGUIT_S,)).fetchone()[0] or snap_fi
    for tid, ts, d, via_c, wps_c in con.execute(
            "SELECT t.track_id, t.ts, t.d, t.via, t.waypoints FROM trenes t "
            "WHERE t.snapshot_id>? AND t.snapshot_id<=? AND t.linea=? AND t.origen=0 "
            "AND t.d IS NOT NULL AND t.track_id<>? AND NOT EXISTS (SELECT 1 FROM trenes u "
            "WHERE u.track_id=t.track_id AND u.ts<t.ts) ORDER BY t.ts",
            (snap_fi, snap_max, linea, track_id)):
        w = json.loads(wps_c or "[]")
        if not w:
            continue
        if (1 if w[-1]["d"] >= w[0]["d"] else -1) != dir_:
            continue
        if not _vies_compatibles(via, via_c or linea, d0, d, ctx.vias):
            continue
        s_pred = (d_previst(wps, ts - ts_fi) - d0) * dir_
        if abs((d - d0) * dir_ - s_pred) <= SEGUIT_M:
            return tid                  # el primer que naix (ORDER BY ts) i encaixa
    return None


def decideix(estacions: list[dict]) -> str:
    """Veredicte a partir del que es va veure a les estacions per davant
    ({pas, cancel_prog, horari, sense_dades?}; veure `calcula`)."""
    llegides = [e for e in estacions if not e.get("sense_dades")]
    k = len(llegides)
    vist = sum(e["pas"] is not None for e in llegides)
    canc = sum(e["cancel_prog"] is not None for e in llegides)
    if not llegides:
        return "sense_dades"
    if not any(e["horari"] for e in llegides):
        return "retirat"
    if canc and not vist:
        # ni una sola estació llegida el va veure passar. Amb «menys de la
        # meitat» eixia cancel·lat un L2 (03/11 09:40) que Fuente del Jarro i El
        # Clot van veure passar a l'hora: l'hora que faltava era d'un altre tren
        return "cancellat"
    if vist >= k / 2:
        return "continua"
    return "indeterminat"


def calcula(con, net, track_id: int, ctx: Context | None = None,
            ahora: int | None = None) -> dict | None:
    """Veredicte d'un tren perdut, o None si no ho és (viu o arribat)."""
    ult = con.execute(
        "SELECT snapshot_id, ts, linea, destino, via, waypoints FROM trenes "
        "WHERE track_id=? ORDER BY ts DESC LIMIT 1", (track_id,)).fetchone()
    if not ult:
        return None
    snap_fi, ts_fi, linea, destino, via, wps = ult
    via = via or linea
    wps = json.loads(wps or "[]")
    if snap_fi == con.execute("SELECT MAX(snapshot_id) FROM trenes").fetchone()[0]:
        return None
    if llega_a_destino(net, linea, via, destino, wps):
        return None
    ahora = ahora or int(time.time())
    base = {"ts_fi": ts_fi, "linea": linea, "destino": destino}

    seg = con.execute("SELECT MIN(ts) FROM snapshots WHERE ts>?", (ts_fi,)).fetchone()[0]
    if seg is None:
        return dict(base, veredicte="pendent", fins=None)
    if seg - ts_fi > HUECO_DADES:
        return dict(base, veredicte="sense_dades", buit=[ts_fi, seg])
    if not destino:
        return dict(base, veredicte="indeterminat", motiu="FGV no en diu el destí")

    davant = []
    for w in wps:
        sid = net.id_por_nombre(w.get("nom") or "")
        eta = w.get("eta")
        if sid is None or eta is None or eta < MIN_ETA or eta >= HORIZONTE:
            continue
        davant.append((sid, ts_fi + int(eta)))
        if len(davant) >= MAX_EST:
            break
    if not davant:
        return dict(base, veredicte="indeterminat", motiu="no tenia estacions per davant")
    fins = max(tk for _, tk in davant) + pasos.HORIZONTE_CANC + pasos.MARGEN_CANC + 120
    if ahora < fins:
        return dict(base, veredicte="pendent", fins=fins)

    dia = dt.date.fromtimestamp(ts_fi).isoformat()
    if ctx is None or ctx.dia != dia:
        ctx = Context(con, net, dia, ahora)
    estacions = []
    for sid, tk in davant:
        a, b = tk - VIST_ABANS, tk + VIST_DESPRES
        if not _continuo(_cobertura(con, sid, a - HUECO_MAX, b + HUECO_MAX), a, b):
            estacions.append({"estacio": net.estaciones[sid].nombre, "previst": _hhmm(tk),
                              "pas": None, "cancel_prog": None, "horari": False,
                              "sense_dades": True})
            continue
        pas = con.execute(
            "SELECT ts_salida FROM pasos_obs WHERE origen_id=? AND linea=? AND +estado='salida' "
            "AND ts_salida BETWEEN ? AND ? ORDER BY abs(ts_salida-?) LIMIT 1",
            (sid, linea, tk - VIST_ABANS, tk + VIST_DESPRES, tk)).fetchone()
        v = pasos._vecina(net, linea, sid, destino, ctx.sent.get((linea, destino)))
        slots = ctx.prog.get((sid, v), []) if v is not None else []
        amb_horari = any(tk - 1800 <= p <= tk + 1800 for p in slots)
        canc = next((c for c in ctx.cancelats(sid)
                     if c["hacia_id"] == v and abs(c["ts_prog"] - tk) <= CANC_FINESTRA), None)
        estacions.append({"estacio": net.estaciones[sid].nombre, "previst": _hhmm(tk),
                          "pas": _hhmm(pas[0]) if pas else None,
                          "cancel_prog": canc["hora"] if canc else None,
                          "horari": amb_horari})
    v_ = decideix(estacions)
    res = dict(base, veredicte=v_, estacions=estacions)
    if v_ == "continua" and wps:
        res["seguit_per"] = _seguit_per(con, net, ctx, track_id, snap_fi, ts_fi, linea, via, wps)
    return res


def veredicte(con, net, track_id: int, ctx: Context | None = None,
              ahora: int | None = None, commit: bool = True) -> dict | None:
    """Com `calcula`, però primer mira (i després guarda) `finals_tren`.
    commit=False: qui crida fa el commit (un dia sencer, a tandes: la SD)."""
    try:
        r = con.execute("SELECT detall FROM finals_tren WHERE track_id=?", (track_id,)).fetchone()
    except sqlite3.OperationalError:    # base sense la taula (el poller la crea en arrancar)
        try:
            init_db(con)
        except sqlite3.OperationalError:
            pass
        r = None
    if r:
        return json.loads(r[0])
    res = calcula(con, net, track_id, ctx, ahora)
    if res and res["veredicte"] in DEFINITIUS:
        try:
            con.execute("INSERT OR REPLACE INTO finals_tren(track_id, ts_fi, linea, destino, "
                        "veredicte, detall) VALUES (?,?,?,?,?,?)",
                        (track_id, res["ts_fi"], res["linea"], res["destino"], res["veredicte"],
                         json.dumps(res, ensure_ascii=False)))
            if commit:
                con.commit()
        except sqlite3.OperationalError:
            pass                        # base ocupada o de només lectura: es tornarà a calcular
    return res


def del_dia(con, net, dia: str, ahora: int | None = None) -> dict:
    """{veredicte: n} dels trens perduts del dia (i la llista)."""
    ahora = ahora or int(time.time())
    t0, t1 = _rango_dia(dia)
    ctx = Context(con, net, dia, ahora)
    ult = con.execute("SELECT MAX(snapshot_id) FROM trenes").fetchone()[0]
    compte: dict[str, int] = {}
    llista = []
    for (tid,) in con.execute(
            "SELECT track_id FROM trenes WHERE ts>=? AND ts<? GROUP BY track_id "
            "HAVING MAX(snapshot_id)<?", (t0, t1, ult)).fetchall():
        r = veredicte(con, net, tid, ctx, ahora, commit=False)
        if len(llista) % 200 == 199:
            con.commit()
        if r is None:
            continue
        compte[r["veredicte"]] = compte.get(r["veredicte"], 0) + 1
        llista.append(dict(r, id=tid))
    con.commit()
    return {"dia": dia, "compte": compte, "trens": llista}


def purga(con, hasta_ts: int) -> int:
    cur = con.execute("DELETE FROM finals_tren WHERE ts_fi<?", (hasta_ts,))
    con.commit()
    return cur.rowcount


def main():
    from .network import load_network
    from .poller import DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--dia", default=dt.date.today().isoformat())
    ap.add_argument("--llista", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(DB, timeout=60)
    init_db(con)
    t = time.time()
    r = del_dia(con, load_network(), a.dia)
    print(a.dia, r["compte"], f"{time.time() - t:.1f} s")
    if a.llista:
        for f in r["trens"]:
            if f["veredicte"] in ("cancellat", "indeterminat"):
                print(f["id"], _hhmm(f["ts_fi"]), f"L{f['linea']}", f["destino"], f["veredicte"],
                      [(e["estacio"], e["previst"], e["pas"], e["cancel_prog"]) for e in f.get("estacions", [])])


if __name__ == "__main__":
    main()

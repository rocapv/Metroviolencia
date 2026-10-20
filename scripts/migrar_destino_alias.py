"""
Aplica DESTINO_ALIAS (backend/parser.py) al histórico ya guardado: trenes,
salidas_obs, pasos_obs y arrivals, y borra el horario de cabecera de hoy y
mañana de las cabeceras afectadas para que se vuelva a descargar con el destino
real (el poller lo rehace en su pasada horaria o: python -m backend.horarios).

Pensado para correr con el poller EN MARCHA: arrivals se actualiza por tandas
de snapshots (índice idx_arr_snap) para no retener el bloqueo de escritura.

    python scripts/migrar_destino_alias.py
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.parser import DESTINO_ALIAS  # noqa: E402
from backend.poller import DB  # noqa: E402
from backend.cabeceras import CABECERAS  # noqa: E402

TANDA = 300   # snapshots por transacción en arrivals


def main() -> None:
    con = sqlite3.connect(DB, timeout=120)
    t = time.time()
    for (linea, viejo), nuevo in DESTINO_ALIAS.items():
        print(f"L{linea} '{viejo}' -> '{nuevo}'")
        for tabla in ("trenes", "salidas_obs", "pasos_obs"):
            cur = con.execute(f"UPDATE {tabla} SET destino=? WHERE linea=? AND destino=?",
                              (nuevo, linea, viejo))
            con.commit()
            print(f"  {tabla}: {cur.rowcount} filas")
        lo, hi = con.execute("SELECT min(snapshot_id), max(snapshot_id) FROM arrivals").fetchone()
        n = 0
        if lo is not None:
            desde = lo
            while desde <= hi:
                fin = min(desde + TANDA - 1, hi)
                cur = con.execute(
                    "UPDATE arrivals SET destino=? WHERE snapshot_id BETWEEN ? AND ? "
                    "AND linea=? AND destino=?", (nuevo, desde, fin, linea, viejo))
                con.commit()
                n += cur.rowcount
                desde = fin + 1
                time.sleep(0.05)
        print(f"  arrivals: {n} filas")
        # cabeceras de esa línea: horario de hoy y mañana a rehacer
        hoy = dt.date.today()
        dias = [hoy.isoformat(), (hoy + dt.timedelta(days=1)).isoformat()]
        for cab in CABECERAS:
            if linea not in cab["lineas"]:
                continue
            for dia in dias:
                con.execute("DELETE FROM horario_prog WHERE dia=? AND origen_id=?", (dia, cab["id"]))
                con.execute("DELETE FROM horario_descargas WHERE dia=? AND origen_id=?", (dia, cab["id"]))
            con.commit()
            print(f"  horario de {cab['nombre']} ({', '.join(dias)}) borrado: se vuelve a descargar")
    con.close()
    print(f"hecho en {time.time() - t:.0f} s")


if __name__ == "__main__":
    main()

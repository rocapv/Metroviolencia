"""
Cabeceras: estaciones donde los trenes de una línea EMPIEZAN su recorrido, y
en las que por tanto todo tren listado en info-estacion es una SALIDA (no un
paso). Elegidas a mano a partir de los datos: en ellas la fuente solo lista
trenes de esas líneas en un único sentido.

Fuera quedan las cabeceras intermedias (València Sud, Empalme, Seminari,
Picassent, Paterna, Machado, Av. del Cid, Almàssera...): ahí se mezclan
salidas con trenes de paso y la fuente no permite distinguirlos. (El
"Benimaclet" que la fuente rotula en L1 no es una cabecera: es Bétera mal
etiquetado; el parser lo corrige, ver parser.DESTINO_ALIAS.)
"""
from __future__ import annotations

import sqlite3
import time

CABECERAS: list[dict] = [
    {"id": 1,   "nombre": "Castelló",             "lineas": [1]},
    {"id": 54,  "nombre": "Llíria",               "lineas": [2]},
    {"id": 107, "nombre": "Torrent Avinguda",     "lineas": [2, 7]},
    {"id": 64,  "nombre": "Rafelbunyol",          "lineas": [3]},
    {"id": 182, "nombre": "Aeroport",             "lineas": [3, 5]},
    {"id": 114, "nombre": "Mas del Rosari",       "lineas": [4]},
    {"id": 115, "nombre": "Ll. Llarga - Terramelar", "lineas": [4]},
    # solo L4: los L6 hacia Tossal salen de Marítim y PASAN por aquí ~10 min
    # después (mismos 51 trenes/día); contarlos daba "retrasos" de +3-7 min falsos
    {"id": 98,  "nombre": "Dr. Lluch",            "lineas": [4]},
    {"id": 132, "nombre": "Tossal del Rei",       "lineas": [6]},
    {"id": 123, "nombre": "Marítim",              "lineas": [5, 6, 7, 8]},
    {"id": 126, "nombre": "Neptú",                "lineas": [8]},
    {"id": 186, "nombre": "Riba-roja de Túria",   "lineas": [9]},
    # cabecera de L9 pero L3 pasa por ella: el planificador mezcla ambas y hay
    # que restar los L3 (que vienen de Rafelbunyol), ver horarios._restar_paso
    {"id": 56,  "nombre": "Alboraia Peris Aragó", "lineas": [9], "paso_de": [64]},
    {"id": 190, "nombre": "Alacant",              "lineas": [10]},
    {"id": 197, "nombre": "Natzaret",             "lineas": [10]},
]
POR_ID = {c["id"]: c for c in CABECERAS}

DIAS_HISTORIA = 7   # ventana para deducir los destinos que salen de cada cabecera
MIN_VISTAS = 5      # apariciones mínimas para contar un destino (quita ruido)


def destinos_de(con: sqlite3.Connection, cab: dict) -> list[tuple[str, int, int]]:
    """[(destino, linea, veces_visto)] que salen de la cabecera según el tiempo real reciente."""
    desde = int(time.time()) - DIAS_HISTORIA * 86400
    marcas = ",".join("?" * len(cab["lineas"]))
    rows = con.execute(
        f"SELECT destino, linea, COUNT(*) FROM arrivals "
        f"WHERE estacion_id=? AND ts>=? AND linea IN ({marcas}) AND destino<>'' "
        f"AND destino<>? GROUP BY destino, linea HAVING COUNT(*)>=?",
        (cab["id"], desde, *cab["lineas"], cab["nombre"], MIN_VISTAS),
    ).fetchall()
    return [(d, l, n) for d, l, n in rows]

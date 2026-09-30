"""backend/parser.py: fragmento HTML de info-estacion -> lista de llegadas."""
from __future__ import annotations

import re

import pytest

from backend.parser import Arrival, parse_arrivals, parse_eta, parse_station_name


def _bloque(linea: int, destino: str, minutos: str) -> str:
    """Un bloque item--proximos con la misma estructura que la web."""
    return f"""
    <div class="estacion sin-enlace w100">
      <div class="info-estacion w100 item--proximos">
        <div class="df-sb w100">
          <div class="df-s">
            <span class="linea linea-{linea}"><img src="x.svg" alt="Línea {linea}"></span>
            <div class="nombre-estacion">{destino}</div>
          </div>
          <div class="df-e">
            <div class="nombre-estacion estacion__espera js-descontar-tiempo fw--600">
              <span class="minutos c-negro">{minutos}</span>
            </div>
          </div>
        </div>
      </div>
    </div>"""


CABECERA = ('<div class="trayecto-calculo trayecto"><div class="df-s pb10">'
            '<h3 class="c-gris-o fs--18 fw--800 detalle-estacion--titular mr10">Prova</h3>'
            '</div><div class="separador-gris mt10 pb10"><div class="row">')
PIE = '</div></div></div>'


# ------------------------------------------------------------- muestra real

def test_muestra_real_estacion_25(html_est25):
    llegadas = parse_arrivals(html_est25)
    assert len(llegadas) == 8
    assert all(isinstance(a, Arrival) for a in llegadas)
    primera = llegadas[0]
    assert (primera.linea, primera.destino, primera.eta_seg, primera.eta_txt) == \
        (9, "Alboraia Peris Aragó", 60, "1 min")
    assert llegadas[-1] == Arrival(3, "Rafelbunyol", 720, "12 min", None)
    # la web ordena el panel por tiempo de llegada
    etas = [a.eta_seg for a in llegadas]
    assert etas == sorted(etas)
    # resolución de minuto, ocupación aún no servida
    assert all(a.eta_seg % 60 == 0 for a in llegadas)
    assert all(a.ocupacion is None for a in llegadas)
    assert {a.linea for a in llegadas} == {1, 2, 3, 5, 9}


def test_nombre_estacion_muestra_real(html_est25):
    assert parse_station_name(html_est25) == "Àngel Guimerà"


def test_to_dict(html_est25):
    d = parse_arrivals(html_est25)[1].to_dict()
    assert d == {"linea": 5, "destino": "Alameda", "eta_seg": 240,
                 "eta_txt": "4 min", "ocupacion": None}


# -------------------------------------------------------------- panel vacío

def test_panel_vacio_sobre_muestra_real(html_est25):
    """Misma cabecera de la web pero sin ningún bloque de llegada (de madrugada
    la fuente devuelve el esqueleto sin trenes)."""
    sin_llegadas = re.sub(r"item--proximos.*", "", html_est25, flags=re.S)
    assert "item--proximos" not in sin_llegadas
    assert parse_arrivals(sin_llegadas) == []
    assert parse_station_name(sin_llegadas) == "Àngel Guimerà"


def test_panel_vacio_sintetico():
    assert parse_arrivals(CABECERA + PIE) == []
    assert parse_arrivals("") == []
    assert parse_station_name("") is None


# ------------------------------------------------------------- ETAs y textos

@pytest.mark.parametrize("txt, seg", [
    ("1 min", 60),
    ("12 min", 720),
    ("  3 MIN ", 180),
    ("1 h 29 min", 5340),
    ("2 h", 7200),
    ("1 h", 3600),
    ("0 min", 0),
    ("Ara", 0),
    ("Pròxim", 0),
    ("En andana", 0),
    ("7", 420),          # solo un número: se interpreta como minutos
    ("", 0),
])
def test_parse_eta(txt, seg):
    assert parse_eta(txt) == seg


def test_bloques_sinteticos_con_entidades_y_etiquetas():
    html = CABECERA + _bloque(4, "Dr.&nbsp;Lluch", "Pròxim") \
        + _bloque(1, "<strong>Bétera</strong>", "1 h 5 min") + PIE
    llegadas = parse_arrivals(html)
    assert [a.linea for a in llegadas] == [4, 1]
    assert llegadas[0].destino == "Dr.\xa0Lluch"      # &nbsp; -> espacio duro, no se pierde
    assert llegadas[0].eta_seg == 0 and llegadas[0].eta_txt == "Pròxim"
    assert llegadas[1].destino == "Bétera"           # etiquetas internas fuera
    assert llegadas[1].eta_seg == 3900


def test_bloque_incompleto_se_descarta():
    """Un bloque sin <span class="minutos"> (o sin línea) no genera llegada."""
    roto = _bloque(2, "Llíria", "5 min").replace('<span class="minutos c-negro">5 min</span>', "")
    html = CABECERA + roto + _bloque(2, "Torrent Avinguda", "9 min") + PIE
    llegadas = parse_arrivals(html)
    assert [(a.linea, a.destino, a.eta_seg) for a in llegadas] == [(2, "Torrent Avinguda", 540)]

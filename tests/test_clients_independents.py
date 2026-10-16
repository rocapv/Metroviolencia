"""Cada cliente es un usuario distinto: el estado de la web no se comparte.

El frontend guarda la ruta elegida en el navegador. Si la guarda en
`localStorage`, TODAS las pestañas y ventanas del mismo navegador comparten
ese valor y, al abrir la web, una ve la ruta que calculó otra (reproducido el
16/10/2026 contra producción). Debe ir en `sessionStorage`, que es por pestaña.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
JS = sorted(WEB.glob("*.js"))


@pytest.mark.parametrize("js", JS, ids=lambda p: p.name)
def test_estado_de_usuario_solo_en_sessionstorage(js: Path):
    """Ningún script escribe estado de usuario en el almacén compartido."""
    src = js.read_text(encoding="utf-8")
    escrituras = re.findall(r"localStorage\.(setItem|clear)\s*\(", src)
    assert not escrituras, (
        f"{js.name} escribe en localStorage {escrituras}: lo comparten todas las "
        "pestañas del navegador y un cliente vería el estado de otro. "
        "Usa sessionStorage."
    )


def test_la_ruta_se_guarda_por_pestana():
    """La ruta (`mv_ruta`) se guarda y se lee de sessionStorage, y la que
    hubiera quedado en el almacén compartido se borra al cargar."""
    src = (WEB / "panells.js").read_text(encoding="utf-8")
    assert "sessionStorage.setItem" in src and "sessionStorage.getItem" in src
    assert 'localStorage.removeItem("mv_ruta")' in src, (
        "hay que limpiar el mv_ruta heredado de versiones que usaban localStorage"
    )

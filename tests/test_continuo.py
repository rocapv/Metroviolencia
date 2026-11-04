"""salidas._continuo: lecturas sin huecos en toda una ventana (04/11/2026)."""
from backend.salidas import _continuo

PAS = [0, 120, 240, 360, 480, 600]


def test_ventana_leida_entera():
    assert _continuo(PAS, 100, 500, hueco=600)


def test_hueco_en_medio():
    # pasada a 233 y la siguiente a 1715 (el poller parado): nada en medio se sabe
    assert not _continuo([0, 120, 233, 1715, 1835], 150, 400, hueco=600)


def test_sin_lectura_despues_del_final():
    # la ventana acaba a 700 y la última lectura conocida es 600: no se sabe si siguió
    assert not _continuo(PAS, 100, 700, hueco=600)


def test_sin_lectura_antes_del_inicio():
    assert not _continuo([700, 820], 0, 800, hueco=600)
    assert not _continuo([], 0, 10)

"""fgv_http.peticio: reintenta amb connexió nova només si no s'obri (06/11/2026)."""
import pytest
import requests

from backend import fgv_http


class Sessio:
    def __init__(self, errors):
        self.errors = list(errors); self.crides = 0; self.timeouts = []

    def get(self, url, timeout=None, **kw):
        self.crides += 1; self.timeouts.append(timeout)
        if self.errors:
            raise self.errors.pop(0)
        return "resposta"

    post = get


def test_reintenta_si_no_connecta():
    s = Sessio([requests.exceptions.ConnectTimeout(), requests.exceptions.ConnectionError()])
    assert fgv_http.peticio(s, "POST", "u") == "resposta"
    assert s.crides == 3 and s.timeouts[0] == (fgv_http.CONNECTA, 30)


def test_es_rendix_al_cap_dels_intents():
    s = Sessio([requests.exceptions.ConnectTimeout()] * 10)
    with pytest.raises(requests.exceptions.ConnectTimeout):
        fgv_http.peticio(s, "GET", "u")
    assert s.crides == fgv_http.INTENTS


def test_no_reintenta_resposta_lenta_ni_tls():
    for e in (requests.exceptions.ReadTimeout(), requests.exceptions.SSLError()):
        s = Sessio([e])
        with pytest.raises(type(e)):
            fgv_http.peticio(s, "GET", "u")
        assert s.crides == 1

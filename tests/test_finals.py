"""finals.decideix: com va acabar un tren perdut a mitjan trajecte (04/11/2026)."""
from backend.finals import decideix


def est(pas=None, canc=None, horari=True, sd=False, horari_dia=True):
    e = {"estacio": "X", "previst": "10:00", "pas": pas, "cancel_prog": canc, "horari": horari,
         "horari_dia": horari_dia}
    if sd:
        e["sense_dades"] = True
    return e


def test_les_seguents_el_veuen_passar():
    assert decideix([est("10:01"), est("10:03"), est()]) == "continua"


def test_cancellat_cap_el_veu_i_falta_al_horari():
    assert decideix([est(canc="09:58"), est()]) == "cancellat"


def test_un_pas_vist_ja_no_es_cancellat():
    # 03/11 09:40, L2 Llíria: 2 de 5 el van veure; l'hora que faltava era d'un altre tren
    e = [est("09:41"), est(canc="09:34"), est(), est(), est("09:56")]
    assert decideix(e) == "indeterminat"


def test_sense_horari_per_davant_es_retirat():
    assert decideix([est(horari=False), est(horari=False)]) == "retirat"


def test_tot_sense_lectures():
    assert decideix([est(sd=True), est(sd=True)]) == "sense_dades"


def test_les_no_llegides_no_compten():
    # el poller parat a la segona: el que diu la primera mana
    assert decideix([est(canc="10:05"), est(sd=True), est(sd=True)]) == "cancellat"
    assert decideix([est("10:05"), est(sd=True)]) == "continua"


def test_vist_passar_sense_horari_va_seguir():
    # 06/11, L4 #624995: Dr. Lluch i Cabanyal el van veure a l'hora; eixes parades
    # no tenen horari i eixia «retirat»
    assert decideix([est("06:19", horari=False, horari_dia=False),
                     est("06:20", horari=False, horari_dia=False)]) == "continua"


def test_sense_horari_ni_pas_no_es_retirat():
    assert decideix([est(horari=False, horari_dia=False)]) == "indeterminat"

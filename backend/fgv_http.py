"""
Peticions a la web de FGV amb connexió curta i reintents.

Des del 05/11/2026 cap a les 10:00, la web de FGV deixa sense resposta una
bona part de les connexions noves: el SYN no torna mai (des de casa i des
d'OVH igual, no és un bloqueig a la nostra IP; parar el poller 3 min no hi
feia res). Pareix un balancejador amb un servidor mort: la connexió s'obri a
l'instant o no s'obri, encara que el nucli repetisca el SYN durant 15-30 s.
Amb 15-30 s d'espera, el 25-52 % de les lectures fallava i una pasada tardava
3 min. Amb 3 s de connexió i una connexió nova a cada intent, 30 de 30
estacions es llegien (17 a la primera, 9 a la segona, 4 a la tercera o
quarta).

Només es reintenta quan la connexió no s'obri: una resposta lenta (el
servidor ja ha acceptat) o un error de TLS no es repetixen.
"""
from __future__ import annotations

import requests

CONNECTA = 3.0     # s per a obrir la connexió
INTENTS = 4        # connexions noves com a màxim


def peticio(session: requests.Session, metode: str, url: str, lectura: float = 30,
            intents: int = INTENTS, **kw) -> requests.Response:
    ultim: Exception | None = None
    for _ in range(max(1, intents)):
        try:
            return getattr(session, metode.lower())(url, timeout=(CONNECTA, lectura), **kw)
        except requests.exceptions.SSLError:
            raise
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
            ultim = e
    raise ultim  # type: ignore[misc]

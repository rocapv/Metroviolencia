"""
Construye data/ca-bundle.pem = CAs de certifi + el certificado intermedio de
Sectigo que www.metrovalencia.es NO envía en su cadena (por eso falla la
verificación TLS en Linux/OpenSSL, que no resuelve AIA automáticamente).

Ejecutar una vez tras instalar dependencias:
    python scripts/build_ca_bundle.py
"""
import ssl
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ca-bundle.pem"
# AIA "CA Issuers" del certificado leaf de www.metrovalencia.es
INTERMEDIATE_URL = (
    "http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt"
)


def main() -> int:
    der = urllib.request.urlopen(INTERMEDIATE_URL, timeout=20).read()
    pem = ssl.DER_cert_to_PEM_cert(der)
    base = Path(certifi.where()).read_text(encoding="utf-8")
    OUT.write_text(base + "\n" + pem, encoding="utf-8")
    print(f"bundle escrito: {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

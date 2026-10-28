#!/usr/bin/env python
"""
Inicio de sesión MANUAL para AzMed / Identity / Veeva Vault / Kaltura.

Abre una ventana del navegador elegido (Chrome, Edge o Chromium) con una pestaña
por plataforma. Inicia sesión a mano (usuario, contraseña y MFA). La sesión se
guarda automáticamente cada 5 segundos en `data/auth_state.json` y el token KS de
KMC en `data/kaltura_session.json`; puedes cerrar el navegador cuando termines.

El crawler reutiliza esa sesión en CUALQUIER sitio del catálogo (AzMed, IDAZ,
Veeva, Kaltura...) y también combina sesiones antiguas de `data/` para no perder
las cookies de otras plataformas.

Uso normal (una sola vez, o cuando caduque la sesión):
    .venv\\Scripts\\python.exe scripts/save_session.py

Solo una plataforma:
    .venv\\Scripts\\python.exe scripts/save_session.py --url https://www.azmed.cr/

Varias plataformas (puedes repetir --url):
    .venv\\Scripts\\python.exe scripts/save_session.py ^
        --url https://www.azmed.cr/ ^
        --url https://login.veevavault.com/auth/login ^
        --url https://kmc.kaltura.com/index.php/kmcng/login

NOTA: `data/auth_state.json` contiene cookies de sesión. No lo compartas ni lo
subas a git.
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.scanner.auth_prep import DEFAULT_URLS, run_sync  # noqa: E402


def cli() -> int:
    parser = argparse.ArgumentParser(
        description="Inicia sesión a mano y guarda la sesión para el AZ Content Scanner"
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="URL de login. Puede repetirse. Por defecto se abren AzMed, Veeva Vault y Kaltura.",
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "chromium", "chrome", "edge"),
        default="auto",
        help="Navegador: auto (Chrome/Edge si están instalados), chromium, chrome o edge.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="No mostrar la ventana (no sirve para MFA; solo para pruebas)",
    )
    args = parser.parse_args()

    urls = args.urls or list(DEFAULT_URLS)
    print("Se abrirá el navegador para iniciar sesión en:")
    for url in urls:
        print(f"  - {url}")
    print()
    print("1) Inicia sesión con SSO/MFA en cada pestaña.")
    print("2) La sesión se guarda sola cada 5 segundos en data/auth_state.json.")
    print("3) Cierra el navegador cuando hayas terminado.")
    print()

    result = run_sync(urls, args.headless, args.browser)
    print()
    print(f"Sesión guardada en: {result.get('path')}")
    print(f"Cookies capturadas: {result.get('cookies', 0)}")
    print("El crawler la usará automáticamente en el próximo escaneo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())

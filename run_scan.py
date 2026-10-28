"""
Uso:
    python run_scan.py --market CAMCAR
    python run_scan.py --all
"""
import argparse
import asyncio
import logging
import sys

# Fix for Playwright on Windows
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from app.database import init_db
from app.scanner.pipeline import run_market_scan, load_markets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")



async def main(markets: list[str]):
    init_db()
    for code in markets:
        run_id = await run_market_scan(code)
        print(f"Escaneo #{run_id} finalizado para {code}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", help="Código de mercado, ej. CAMCAR")
    ap.add_argument("--all", action="store_true", help="Escanear todos los mercados")
    args = ap.parse_args()

    targets = list(load_markets().keys()) if args.all else ([args.market] if args.market else [])
    if not targets:
        ap.error("Indica --market CODIGO o --all")
    asyncio.run(main(targets))
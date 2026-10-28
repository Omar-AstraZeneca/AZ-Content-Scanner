import logging
import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import scan, assets, metrics, auth
from app.routers.scan import close_orphan_runs

from app.scanner.image_analyzer import tesseract_available
from app.security import security_middleware
from app.config import get_settings

# Fix for Playwright on Windows
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")



@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    startup_log = logging.getLogger("startup")
    # Si el servidor se cayó durante un escaneo, ese run quedó en RUNNING para siempre.
    # Al arrancar no puede haber ninguno en ejecución, así que se cierran como ERROR.
    closed = close_orphan_runs()
    if closed:
        startup_log.warning("Se cerraron %s escaneo(s) huérfano(s)", closed)
    if not get_settings().dashboard_password:
        startup_log.warning(
            "DASHBOARD_PASSWORD no está definida: solo se aceptan conexiones desde esta máquina. "
            "Define una contraseña en el .env antes de exponer el servicio en red."
        )
    if not tesseract_available():
        startup_log.error(
            "TESSERACT NO ESTÁ INSTALADO: las imágenes no se leerán. "
            "Instala Tesseract (Windows: UB Mannheim, macOS: brew install tesseract tesseract-lang, "
            "Linux: tesseract-ocr) y reinicia el servidor. Configura TESSERACT_CMD en .env si es necesario."
        )

    try:
        yield
    finally:
        # Si uvicorn se apaga con la ventana de login abierta, no debe quedar
        # Chromium huérfano. Solo cierra el proceso que lanzó esta API.
        try:
            from app.routers.auth import stop_auth_prep_process

            stop_auth_prep_process()
        except Exception:
            pass


app = FastAPI(title="AZ Content Expiry Scanner (Pilot)", version="0.1.0", lifespan=lifespan)

# Seguridad: host, autenticación, anti-CSRF y cabeceras (se ejecuta antes que cualquier ruta)
app.middleware("http")(security_middleware)

app.include_router(scan.router)
app.include_router(assets.router)
app.include_router(metrics.router)
app.include_router(auth.router)

# El frontend se sirve al final para no tapar las rutas /api
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

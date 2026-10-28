"""
Seguridad de la API y del dashboard (middleware).

1. Host permitido (ALLOWED_HOSTS): frena el DNS rebinding, con el que una web maliciosa
   podría hablar con tu servidor local usando un nombre de dominio que ella controla.
2. Autenticación HTTP Basic (DASHBOARD_PASSWORD). Sin contraseña configurada, la aplicación
   SOLO acepta conexiones desde la propia máquina y rechaza peticiones con cabeceras de proxy
   (un proxy inverso en la misma máquina haría que todo el mundo pareciera «local»).
3. Anti-CSRF: las peticiones que modifican datos desde un navegador deben venir del mismo
   origen. Sin esto, cualquier página web podría lanzar o cancelar escaneos en tu servidor.
4. Límite de intentos fallidos de login por IP (fuerza bruta).
5. Cabeceras de seguridad (CSP, nosniff, anti-clickjacking, sin caché en la API).
"""
import hmac
import logging
import time
from base64 import b64decode
from binascii import Error as Base64Error
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

log = logging.getLogger("security")

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost", "testclient"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")

MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 300
_failures: dict[str, deque] = defaultdict(deque)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _host_only(header: str) -> str:
    header = (header or "").strip().lower()
    if header.startswith("["):                       # IPv6: [::1]:8000
        return header[: header.find("]") + 1] if "]" in header else header
    return header.split(":", 1)[0]


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in get_settings().allowed_hosts.split(",") if h.strip()}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _too_many_failures(ip: str) -> bool:
    now = time.monotonic()
    bucket = _failures[ip]
    while bucket and now - bucket[0] > FAILURE_WINDOW_SECONDS:
        bucket.popleft()
    return len(bucket) >= MAX_FAILURES


def reset_rate_limits() -> None:
    _failures.clear()


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (Base64Error, UnicodeDecodeError, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


def _deny(status: int, detail: str, *, challenge: bool = False) -> JSONResponse:
    response = JSONResponse({"detail": detail}, status_code=status)
    if challenge:
        response.headers["WWW-Authenticate"] = 'Basic realm="AZ Content Scanner", charset="UTF-8"'
    return response


async def security_middleware(request: Request, call_next):
    settings = get_settings()

    # 1) Host permitido
    if _host_only(request.headers.get("host", "")) not in _allowed_hosts():
        return _deny(400, "Host no permitido. Añádelo a ALLOWED_HOSTS si es legítimo.")

    # 2) Autenticación
    user: str | None = None
    ip = _client_ip(request)
    if settings.dashboard_password:
        if _too_many_failures(ip):
            return _deny(429, "Demasiados intentos fallidos. Espera unos minutos.")
        creds = _parse_basic(request.headers.get("authorization"))
        # Se comparan SIEMPRE usuario y contraseña, en tiempo constante
        ok = False
        if creds:
            user_ok = hmac.compare_digest(creds[0].encode(), settings.dashboard_user.encode())
            pass_ok = hmac.compare_digest(creds[1].encode(), settings.dashboard_password.encode())
            ok = user_ok and pass_ok
        if not ok:
            if creds:
                _failures[ip].append(time.monotonic())
                log.warning("Login fallido desde %s", ip)
            return _deny(401, "Autenticación requerida", challenge=True)
        user = creds[0]
    else:
        if ip not in LOOPBACK or any(h in request.headers for h in PROXY_HEADERS):
            return _deny(
                403,
                "Acceso remoto deshabilitado: define DASHBOARD_PASSWORD en el .env para exponer el servicio.",
            )
    request.state.user = user

    # 3) Anti-CSRF para peticiones que modifican datos
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if origin is not None:
            if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                return _deny(403, "Origen no permitido")
        elif site in ("cross-site", "same-site"):
            return _deny(403, "Origen no permitido")

    response = await call_next(request)

    # 5) Cabeceras de seguridad
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

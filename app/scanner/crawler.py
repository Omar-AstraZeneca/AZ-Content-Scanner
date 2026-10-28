"""
Crawler con Playwright: recorre páginas del mismo dominio, renderiza JS,
captura screenshots como evidencia y devuelve HTML + texto visible.

Rastreo completo y reproducible:
  - Sin límite de páginas ni de profundidad por defecto (MAX_PAGES_PER_SCAN=0,
    MAX_DEPTH=0). Recorre todo lo alcanzable desde la URL elegida dentro de la
    lista de dominios autorizados (incluye subdominios).
  - Los enlaces se ordenan antes de encolarse, así dos escaneos visitan las
    mismas páginas en el mismo orden.
  - Se normalizan las URL para no visitar dos veces la misma página.

Páginas protegidas (post-login):
  1) Si hay una sesión guardada (scripts/save_session.py) se usa; es la vía recomendada
     porque permite resolver el MFA a mano.
  2) Si no hay sesión o caducó, y existen credenciales de una CUENTA DE SERVICIO en el
     .env, el crawler completa el formulario del Identity UNA vez por escaneo.
  3) Si el login falla, las páginas que redirigen a Identity se registran como
     «requiere autenticación» (no como fallo). El escaneo se detiene de forma ordenada
     cuando se supera el límite configurable (CRAWLER_MAX_AUTH_PAGES seguidas o
     CRAWLER_MAX_AUTH_TOTAL en total) y lo detectado hasta ese punto se conserva.

Fallos de red: la apertura de cada página se reintenta (3 intentos) ante tiempos agotados
o caídas de conexión, para no perder un sitio entero por un fallo pasajero.

Imágenes y PDFs: se obtienen con el propio navegador (sesión, cookies y cabeceras
reales). Así se evita el 403 que muchos sitios (AEM + CDN) devuelven a clientes
que no son un navegador, como `requests`. Se analizan todos los formatos, incluidos
los SVG (extrayendo su texto) y las imágenes de srcset, <picture>, lazy-load,
og:image, fondos CSS y pósters de vídeo.

IMPORTANTE (seguridad): este módulo puede iniciar sesión en portales HCP con una
cuenta de servicio. Debe usarse solo con autorización de IT/Seguridad, con
credenciales dedicadas guardadas en .env o en un gestor de secretos. Las
credenciales SOLO se escriben en los hosts de Identity configurados y nunca en
escaneos de URLs externas.
"""
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

# Fix for Playwright on Windows
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from playwright.async_api import async_playwright, Page

from app.config import get_settings
from app.scanner.kaltura_session import (
    init_script as kaltura_init_script,
    ks_available,
    prime_page as prime_kaltura_page,
)
from app.scanner.netguard import host_is_public_async, is_public_http_url  # noqa: F401 (re-export)

settings = get_settings()
log = logging.getLogger("crawler")


# Hosts de Identity conocidos. Se pueden ampliar con CRAWLER_IDENTITY_HOSTS (coma).
AZMED_IDENTITY_HOST = "identity.astrazeneca.com"
AZMED_IDENTITY_HINTS = (
    "identity.astrazeneca.com",
    "login.astrazeneca.com",
    "sso.astrazeneca.com",
)

SKIP_EXTENSIONS = (
    ".css", ".js", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".xml", ".json",
    ".ico",
)


# Parámetros de seguimiento que no cambian el contenido de la página
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "_ga"}

# Límites para los recursos que se capturan en memoria.
# El mínimo es bajo a propósito: un banner con texto puede pesar poco. Lo que se descarta
# por tamaño en el análisis es lo que mide menos de MIN_IMAGE_WIDTH x MIN_IMAGE_HEIGHT.
MIN_IMAGE_BYTES = 500
MAX_IMAGE_BYTES = 8_000_000
MAX_PDF_BYTES = 25_000_000

# Marca con la que se identifica una página protegida (la usa el pipeline)
AUTH_REQUIRED = "REQUIERE_AUTENTICACION"

# Marca que indica que el escaneo se detuvo porque no hay una sesión válida (la usa el pipeline)
AUTH_STOPPED = "ESCANEO_DETENIDO_SIN_SESION"

# Archivo con la sesión del navegador que guarda scripts/save_session.py
# (rutas absolutas: el servidor puede arrancarse desde otra carpeta)
BASE_DIR = Path(__file__).resolve().parents[2]
SESSION_FILE = str(BASE_DIR / "data" / "auth_state.json")
SESSION_FILE_ALT = str(BASE_DIR / "data" / "auth_state_promomats.json")
SESSION_FILE_ALT2 = str(BASE_DIR / "data" / "auth_state_veeva.json")
SESSION_FILE_ALT3 = str(BASE_DIR / "data" / "auth_state_kaltura.json")
SESSION_FILES = (SESSION_FILE, SESSION_FILE_ALT, SESSION_FILE_ALT2, SESSION_FILE_ALT3)

# Hosts de los proveedores de identidad que pueden aparecer al abrir un sitio con
# sesión guardada. Se permiten en las peticiones para que el SSO pueda renovar la
# sesión; no se usan para escribir credenciales salvo que CRAWLER_LOGIN_* lo permita.
AUTH_PROVIDER_HOSTS = (
    "identity.astrazeneca.com",
    "login.astrazeneca.com",
    "sso.astrazeneca.com",
    "login.veevavault.com",
    "login.microsoftonline.com",
    "login.live.com",
    "b2clogin.com",
)

# Intentos para abrir una página ante fallos de red pasajeros
GOTO_ATTEMPTS = 3

# Fragmentos de error de Playwright/Chromium que justifican reintentar
TRANSIENT_ERRORS = (
    "ERR_TIMED_OUT", "ERR_CONNECTION", "ERR_NETWORK", "ERR_NAME_NOT_RESOLVED",
    "ERR_INTERNET_DISCONNECTED", "ERR_EMPTY_RESPONSE", "Timeout",
)

# Un host «parece un dominio» si contiene un punto seguido de una extensión alfabética
LOOKS_LIKE_DOMAIN = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.IGNORECASE)

# Selectores razonables si el .env no define los propios
DEFAULT_USER_SELECTORS = (
    'input[name="username"],input[name="email"],input[type="email"],'
    'input[autocomplete="username"],input[id*="user" i],input[name*="user" i],'
    'input[id*="login" i],input[name*="login" i]'
)
DEFAULT_PASSWORD_SELECTORS = (
    'input[type="password"],input[name="password"],input[id*="password" i],'
    'input[name*="password" i]'
)
DEFAULT_SUBMIT_SELECTORS = (
    'form button[type="submit"],form input[type="submit"],button[type="submit"],'
    'input[type="submit"],form button:not([type])'
)


def _setting(name: str, default=""):
    """Lee un ajuste opcional sin fallar si no existe en config.py."""
    value = getattr(settings, name, default)
    return default if value is None else value


def browser_user_agent() -> str | None:
    """Devuelve el User-Agent solo si parece un navegador real.

    Un UA tipo «AZ-Content-Scanner/1.0» hace que los WAF de AEM/Akamai bloqueen la
    petición o la dejen colgada hasta agotar el tiempo de espera. En ese caso se
    devuelve None para que Playwright use el UA nativo de Chromium.
    """
    ua = str(_setting("user_agent", "") or "").strip()
    if not ua or not re.search(r"mozilla|webkit|chrome|safari|firefox|edg", ua, re.IGNORECASE):
        return None
    return ua


@dataclass
class PageResult:
    url: str
    html: str
    visible_text: str
    footer_text: str
    screenshot_path: str | None
    links: list[str]
    pdf_links: list[str]
    depth: int = 0
    error: str | None = None
    # True si la página exige iniciar sesión (no es un fallo: es contenido protegido)
    auth_required: bool = False
    # Bytes obtenidos por el navegador: url -> contenido
    image_bytes: dict[str, bytes] = field(default_factory=dict)
    pdf_bytes: dict[str, bytes] = field(default_factory=dict)
    # Todas las URLs de imagen detectadas (img, srcset, picture, og:image, fondos CSS)
    image_urls: list[str] = field(default_factory=list)


def is_vector_image(url: str, content_type: str = "") -> bool:
    """True para SVG. Se analizan igualmente: un SVG puede contener texto con el ID."""
    return False


def _normalize(url: str) -> str:
    """Forma canónica de una URL: sin fragmento, sin parámetros de seguimiento,
    con parámetros ordenados y sin barra final. Evita visitar duplicados."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ))
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _is_malformed_link(absolute: str) -> bool:
    """Detecta enlaces mal escritos en el sitio, por ejemplo href="www.camcar.astrazeneca.com"
    escrito sin https://, que el navegador resuelve como /es/home/www.camcar.astrazeneca.com."""
    last_segment = urlsplit(absolute).path.rstrip("/").rsplit("/", 1)[-1]
    return bool(last_segment) and "." in last_segment and bool(LOOKS_LIKE_DOMAIN.match(last_segment)) \
        and not last_segment.lower().endswith((".html", ".htm", ".php", ".aspx", ".pdf"))


def is_allowed_url(url: str, allowlist: list[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False
    return any(
        host == domain.lower().strip(".")
        or host.endswith("." + domain.lower().strip("."))
        for domain in allowlist
    )


def is_azmed_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return "azmed" in host or "azmedical" in host


def uses_azmed_auth(url: str, allowlist: list[str]) -> bool:
    return is_allowed_url(url, allowlist) and any(
        is_azmed_host(domain) and is_allowed_url(url, [domain])
        for domain in allowlist
    )


# ------------------------------------------------------------------ Identity (inicio de sesión)
def identity_hosts() -> set[str]:
    """Hosts del Identity corporativo: los conocidos + CRAWLER_IDENTITY_HOSTS."""
    hosts = {h.lower() for h in AZMED_IDENTITY_HINTS}
    for item in str(_setting("crawler_identity_hosts")).split(","):
        item = item.strip().lower().rstrip(".")
        if item:
            hosts.add(item)
    return hosts


def auth_provider_hosts() -> set[str]:
    """Todos los hosts de login que pueden aparecer durante un SSO con sesión guardada."""
    return {host.lower() for host in (*AUTH_PROVIDER_HOSTS, *identity_hosts())}


def is_kaltura_host(value: str) -> bool:
    """True para kmc.kaltura.com y cualquier subdominio de kaltura.com."""
    value = (value or "").strip().lower()
    host = urlparse(value).hostname or value
    return host == "kaltura.com" or host.endswith(".kaltura.com")


def saved_session_files() -> list[str]:
    """Archivos de sesión existentes, en orden de prioridad."""
    return [path for path in SESSION_FILES if os.path.exists(path)]


def load_saved_session() -> dict | None:
    """Combina TODAS las sesiones guardadas en un único estado de Playwright.

    Así una sesión antigua de Veeva/Kaltura no se pierde cuando se crea una nueva
    de AzMed (y viceversa): el crawler recibe las cookies de todas las plataformas.
    El archivo principal (auth_state.json) tiene prioridad sobre los de respaldo.
    """
    files = saved_session_files()
    if not files:
        return None
    cookies: dict[tuple, dict] = {}
    origins: dict[str, dict] = {}
    # De menor a mayor prioridad: el archivo principal se aplica al final
    for path in reversed(files):
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("No se pudo leer la sesión %s: %s", path, exc)
            continue
        for cookie in state.get("cookies", []) or []:
            key = (
                cookie.get("name"),
                (cookie.get("domain") or "").lower(),
                cookie.get("path") or "/",
            )
            cookies[key] = cookie
        for origin in state.get("origins", []) or []:
            name = origin.get("origin")
            if not name:
                continue
            merged = origins.setdefault(name, {"origin": name, "localStorage": []})
            existing = {item.get("name"): item for item in merged.get("localStorage", [])}
            for item in origin.get("localStorage", []) or []:
                existing[item.get("name")] = item
            merged["localStorage"] = list(existing.values())
    if not cookies:
        return None
    return {"cookies": list(cookies.values()), "origins": list(origins.values())}


def request_allowlist(allowlist: list[str], allow_azmed_login: bool) -> list[str]:
    """Hosts permitidos para las peticiones del navegador (incluye el Identity si hay login)."""
    if allow_azmed_login and any(is_azmed_host(domain) for domain in allowlist):
        return list(dict.fromkeys([*allowlist, *identity_hosts()]))
    return allowlist


def identity_username() -> str:
    """Usuario del Identity. Acepta las variables nuevas y las antiguas (compatibilidad)."""
    return str(_setting("crawler_login_username") or _setting("crawler_http_username"))


def identity_password() -> str:
    return str(_setting("crawler_login_password") or _setting("crawler_http_password"))


def has_generic_login_credentials() -> bool:
    return bool(identity_username() and identity_password())


def login_hosts() -> set[str]:
    """Hosts donde se permite escribir CRAWLER_LOGIN_*: el de CRAWLER_LOGIN_URL y los de
    CRAWLER_LOGIN_HOSTS. Sin configuración explícita NO se envía el login a ningún sitio."""
    hosts = set()
    login_url = _setting("crawler_login_url")
    if login_url:
        host = (urlparse(login_url).hostname or "").lower().rstrip(".")
        if host:
            hosts.add(host)
    for item in str(_setting("crawler_login_hosts")).split(","):
        item = item.strip().lower().rstrip(".")
        if item:
            hosts.add(item)
    return hosts


def may_send_login(page_url: str, allowlist: list[str], allow_credentials: bool, hosts: set[str]) -> bool:
    """El formulario de login solo se rellena si el escaneo es de un mercado catalogado (no de
    una URL externa confirmada a mano), la página está en la lista autorizada y su host es
    exactamente uno de los hosts de login configurados."""
    host = (urlparse(page_url).hostname or "").lower().rstrip(".")
    return bool(allow_credentials and host in hosts and is_allowed_url(page_url, allowlist))


def _selector_list(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _on_identity_host(page: Page) -> bool:
    """True si la pestaña está en una pantalla de acceso (Identity, Veeva, Microsoft...)."""
    host = (urlparse(page.url).hostname or "").lower().rstrip(".")
    return host in auth_provider_hosts()


async def _first_present(page: Page, selectors: list[str]):
    """Devuelve el primer localizador que exista en la página, o None."""
    for selector in selectors:
        try:
            if await page.locator(selector).count():
                return page.locator(selector).first
        except Exception:
            continue
    return None


async def _first_visible(page: Page, selectors: list[str]):
    """Como _first_present, pero exige que el elemento sea visible (evita campos ocultos)."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            total = await locator.count()
            for index in range(min(total, 5)):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        except Exception:
            continue
    return None


async def _click_and_settle(page: Page, locator) -> None:
    """Hace clic y espera a que la navegación se estabilice (sin fallar si no navega)."""
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            await locator.click(timeout=15000)
    except Exception:
        # Algunos Identity actualizan la misma página por AJAX en lugar de navegar
        try:
            await locator.click(timeout=15000)
        except Exception:
            pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


async def login_to_identity(page: Page, allowlist: list[str]) -> bool:
    """Completa el formulario del Identity corporativo con la cuenta de servicio.

    Admite los dos diseños habituales:
      - una sola pantalla (usuario + contraseña)
      - dos pantallas (primero el usuario, «Siguiente», luego la contraseña)
    Devuelve True si, al terminar, la pestaña ya no está en el Identity.
    No admite MFA dinámico (TOTP): con segundo factor, usa scripts/save_session.py.
    """
    if not has_generic_login_credentials():
        return False
    if not _on_identity_host(page):
        return False

    user_selectors = _selector_list(_setting("crawler_login_field_user") or DEFAULT_USER_SELECTORS)
    password_selectors = _selector_list(_setting("crawler_login_field_password") or DEFAULT_PASSWORD_SELECTORS)
    submit_selectors = _selector_list(_setting("crawler_login_submit") or DEFAULT_SUBMIT_SELECTORS)

    username_field = await _first_visible(page, user_selectors)
    if username_field is None:
        log.warning("Identity: no se encontró el campo de usuario")
        return False

    # Las credenciales solo se escriben si el formulario envía a un host del Identity
    # o a la lista autorizada. Nunca a un tercero.
    try:
        action = await username_field.evaluate("el => (el.form && el.form.action) || ''")
    except Exception:
        action = ""
    if action:
        action_host = (urlparse(action).hostname or "").lower().rstrip(".")
        if action_host not in identity_hosts() and not is_allowed_url(action, allowlist):
            log.warning("Login omitido: el formulario envía a un host no autorizado (%s)", action_host)
            return False

    await username_field.fill(identity_username())

    password_field = await _first_visible(page, password_selectors)
    if password_field is None:
        # Diseño en dos pasos: se envía el usuario y la contraseña aparece después
        submit = await _first_visible(page, submit_selectors)
        if submit is None:
            log.warning("Identity: no se encontró el botón para continuar")
            return False
        await _click_and_settle(page, submit)
        password_field = await _first_visible(page, password_selectors)
        if password_field is None:
            log.warning("Identity: no apareció el campo de contraseña")
            return False

    await password_field.fill(identity_password())
    submit = await _first_visible(page, submit_selectors)
    if submit is None:
        log.warning("Identity: no se encontró el botón de inicio de sesión")
        return False
    await _click_and_settle(page, submit)

    # Segundo factor: solo si hay un código configurado y existe el campo.
    # Un código fijo solo sirve para cuentas con OTP estático; para TOTP usa save_session.py.
    mfa_code = str(_setting("crawler_mfa_code"))
    otp_selectors = _selector_list(_setting("crawler_mfa_field"))
    if mfa_code and otp_selectors and _on_identity_host(page):
        otp = await _first_visible(page, otp_selectors)
        if otp is not None:
            await otp.fill(mfa_code)
            submit = await _first_visible(page, submit_selectors)
            if submit is not None:
                await _click_and_settle(page, submit)

    logged_in = not _on_identity_host(page)
    if not logged_in:
        log.warning("Identity: el inicio de sesión no se completó (¿credenciales, MFA o selectores?)")
    return logged_in


async def _login_on_site(page: Page, allowlist: list[str]) -> bool:
    """Login por formulario en una página del propio sitio (host de CRAWLER_LOGIN_URL).

    Es el flujo antiguo `_attempt_generic_login`, conservado para sitios que no usan el
    Identity corporativo. Solo rellena el formulario si éste envía a un host autorizado.
    """
    if not has_generic_login_credentials():
        return False

    user_selectors = _selector_list(_setting("crawler_login_field_user") or DEFAULT_USER_SELECTORS)
    password_selectors = _selector_list(_setting("crawler_login_field_password") or DEFAULT_PASSWORD_SELECTORS)
    submit_selectors = _selector_list(_setting("crawler_login_submit") or DEFAULT_SUBMIT_SELECTORS)
    otp_selectors = _selector_list(_setting("crawler_mfa_field"))

    username = await _first_present(page, user_selectors)
    password = await _first_present(page, password_selectors)
    if username is None or password is None:
        return False

    # El formulario debe enviarse a un host autorizado: nunca se escriben credenciales en un
    # formulario que apunte a otro dominio.
    try:
        action = await password.evaluate("el => (el.form && el.form.action) || ''")
    except Exception:
        action = ""
    if action and not is_allowed_url(action, allowlist):
        log.warning("Login omitido: el formulario envía a un host no autorizado (%s)", urlparse(action).hostname)
        return False

    await username.fill(identity_username())
    await password.fill(identity_password())

    submit = await _first_present(page, submit_selectors)
    if submit is None:
        return False
    await _click_and_settle(page, submit)

    mfa_code = str(_setting("crawler_mfa_code"))
    if mfa_code:
        otp = await _first_present(page, otp_selectors)
        if otp is not None:
            await otp.fill(mfa_code)
            submit = await _first_present(page, submit_selectors)
            if submit is not None:
                await _click_and_settle(page, submit)
    return True


# ------------------------------------------------------------------ protecciones de red
async def _guard_request(route, allowlist: list[str], strict_public: bool = False) -> None:
    """Bloquea cualquier petición del navegador a dominios fuera de la lista.

    Con `strict_public` (escaneos de URLs externas) además exige que el host resuelva a IPs
    públicas en CADA petición, lo que frena redirecciones o recursos hacia la red interna."""
    url = route.request.url
    if not is_allowed_url(url, allowlist):
        await route.abort()
    elif strict_public and not await host_is_public_async(urlparse(url).hostname):
        await route.abort()
    else:
        await route.continue_()


async def _goto_with_retry(page: Page, url: str, attempts: int = GOTO_ATTEMPTS) -> None:
    """Abre la página con reintentos: un tiempo agotado aislado no debe perder un sitio entero.

    Solo se reintenta ante fallos de red; un error de navegación definitivo no mejora al repetir.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=settings.page_timeout_ms)
            return
        except Exception as exc:
            last_error = exc
            transient = any(code in str(exc) for code in TRANSIENT_ERRORS)
            if not transient or attempt == attempts:
                break
            wait = attempt * 3
            log.warning("Intento %s/%s fallido en %s; reintentando en %ss", attempt, attempts, url, wait)
            await asyncio.sleep(wait)
    assert last_error is not None
    raise last_error


async def _try_accept_cookies(page: Page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Aceptar todas')",
        "button:has-text('Aceptar')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Aceitar')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=1500)
                await asyncio.sleep(0.5)
                return
        except Exception:
            continue


async def _auto_scroll(page: Page) -> None:
    """Fuerza carga de imágenes lazy-load."""
    await page.evaluate(
        """async () => {
            await new Promise(resolve => {
                let total = 0; const step = 600;
                const timer = setInterval(() => {
                    window.scrollBy(0, step); total += step;
                    if (total >= document.body.scrollHeight) { clearInterval(timer); resolve(); }
                }, 120);
            });
            window.scrollTo(0, 0);
        }"""
    )


def _make_response_capturer(images: dict[str, bytes], pdfs: dict[str, bytes], allowlist: list[str]):
    """Crea el manejador que guarda imágenes y PDFs que el navegador descarga."""

    async def capture(response) -> None:
        try:
            if response.status != 200:
                return
            if not is_allowed_url(response.url, allowlist):
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type.startswith("image/"):
                body = await response.body()
                if MIN_IMAGE_BYTES < len(body) < MAX_IMAGE_BYTES:
                    images[response.url] = body
            elif "application/pdf" in content_type:
                body = await response.body()
                if len(body) < MAX_PDF_BYTES:
                    pdfs[response.url] = body
        except Exception:
            # Un recurso que falla no debe detener el análisis de la página
            pass

    return capture


# Recoge TODAS las imágenes de la página, no solo <img src>:
# srcset, <picture><source>, lazy-load, og:image, fondos CSS y pósters de vídeo.
COLLECT_IMAGES_JS = r"""
() => {
    const urls = new Set();
    const add = (value) => {
        if (!value) return;
        const parts = String(value).split(',');
        for (const part of parts) {
            const candidate = part.trim().split(/\s+/)[0];
            if (candidate) urls.add(candidate);
        }
    };

    for (const img of document.querySelectorAll('img')) {
        add(img.currentSrc);
        add(img.getAttribute('src'));
        add(img.getAttribute('srcset'));
        add(img.getAttribute('data-src'));
        add(img.getAttribute('data-srcset'));
        add(img.getAttribute('data-lazy-src'));
        add(img.getAttribute('data-original'));
        add(img.getAttribute('data-image'));
    }
    for (const source of document.querySelectorAll('picture source, source[type^="image"]')) {
        add(source.getAttribute('srcset'));
        add(source.getAttribute('src'));
    }
    for (const link of document.querySelectorAll('link[rel~="preload"][as="image"], link[rel~="icon"]')) {
        add(link.getAttribute('href'));
    }
    for (const meta of document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]')) {
        add(meta.getAttribute('content'));
    }
    for (const video of document.querySelectorAll('video')) {
        add(video.getAttribute('poster'));
    }
    for (const el of document.querySelectorAll('[style*="background"]')) {
        const bg = el.style && el.style.backgroundImage;
        if (bg && bg !== 'none') {
            const matches = bg.match(/url\((['"]?)(.*?)\1\)/g) || [];
            for (const match of matches) {
                add(match.replace(/^url\((['"]?)/, '').replace(/['"]?\)$/, ''));
            }
        }
    }
    return Array.from(urls);
}
"""


async def _collect_image_urls(page: Page, page_url: str, allowlist: list[str]) -> list[str]:
    """Lista absoluta y deduplicada de imágenes presentes en la página."""
    try:
        raw = await page.evaluate(COLLECT_IMAGES_JS)
    except Exception:
        return []
    result = []
    seen = set()
    for item in raw or []:
        if not item or str(item).startswith("data:"):
            continue
        absolute = urljoin(page_url, str(item))
        if not absolute.startswith(("http://", "https://")):
            continue
        if not is_allowed_url(absolute, allowlist):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        result.append(absolute)
    return result



async def _get_following_safely(page: Page, url: str, referer: str, allowlist: list[str],
                                strict_public: bool, hops: int = 3, timeout_ms: int = 10000):
    """GET con la sesión del navegador SIN seguir redirecciones automáticamente: cada salto
    se valida contra la lista autorizada (y contra IPs públicas si `strict_public`)."""
    current = url
    for _ in range(hops + 1):
        if not is_allowed_url(current, allowlist):
            return None
        if strict_public and not await host_is_public_async(urlparse(current).hostname):
            return None
        reply = await page.context.request.get(
            current, headers={"Referer": referer}, timeout=timeout_ms, max_redirects=0
        )
        if reply.status in (301, 302, 303, 307, 308):
            location = reply.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return reply if reply.ok else None
    return None


# Tope de recursos extra por página para que una página con cientos de imágenes
# ocultas no eternice el escaneo. Las que el navegador ya descargó no cuentan aquí.
MAX_EXTRA_IMAGE_FETCHES = 150
MAX_EXTRA_PDF_FETCHES = 40


async def _fetch_missing_resources(
    page: Page,
    page_url: str,
    allowlist: list[str],
    images: dict[str, bytes],
    pdfs: dict[str, bytes],
    strict_public: bool = False,
    image_urls: list[str] | None = None,
) -> None:
    """Pide con la sesión del navegador las imágenes y PDFs que la página no llegó a
    descargar por sí sola (carruseles, pestañas ocultas, enlaces a PDF sin abrir).

    Usa page.context.request: cookies y cabeceras reales, por lo que no recibe el
    403 que sí recibe `requests`. Es el mismo acceso que tiene cualquier visitante.

    `image_urls` evita volver a evaluar el JS de recolección. Las peticiones se
    hacen en paralelo (6 a la vez) con un tope por página.
    """
    try:
        pdf_candidates = await page.eval_on_selector_all(
            "a[href$='.pdf'], a[href*='.pdf?']",
            "els => els.map(e => e.href)",
        )
    except Exception:
        pdf_candidates = []

    if image_urls is None:
        image_urls = await _collect_image_urls(page, page_url, allowlist)

    pending_images = [u for u in image_urls if u not in images][:MAX_EXTRA_IMAGE_FETCHES]
    semaphore = asyncio.Semaphore(6)

    async def fetch_image(resource_url: str) -> None:
        async with semaphore:
            try:
                reply = await _get_following_safely(
                    page, resource_url, page_url, allowlist, strict_public, timeout_ms=8000
                )
                if reply is None:
                    return
                content_type = (reply.headers.get("content-type") or "").lower()
                if not content_type.startswith("image/"):
                    return
                body = await reply.body()
                if MIN_IMAGE_BYTES < len(body) < MAX_IMAGE_BYTES:
                    images[resource_url] = body
            except Exception:
                return

    if pending_images:
        await asyncio.gather(*(fetch_image(u) for u in pending_images))

    pending_pdfs = []
    seen_pdfs = set()
    for resource_url in sorted({u for u in pdf_candidates if u}):
        absolute = urljoin(page_url, resource_url)
        if absolute in pdfs or absolute in seen_pdfs:
            continue
        if not is_allowed_url(absolute, allowlist):
            continue
        seen_pdfs.add(absolute)
        pending_pdfs.append(absolute)
    pending_pdfs = pending_pdfs[:MAX_EXTRA_PDF_FETCHES]

    async def fetch_pdf(resource_url: str) -> None:
        async with semaphore:
            try:
                reply = await _get_following_safely(
                    page, resource_url, page_url, allowlist, strict_public, timeout_ms=10000
                )
                if reply is None:
                    return
                content_type = (reply.headers.get("content-type") or "").lower()
                if "application/pdf" not in content_type:
                    return
                body = await reply.body()
                if len(body) < MAX_PDF_BYTES:
                    pdfs[resource_url] = body
            except Exception:
                return

    if pending_pdfs:
        await asyncio.gather(*(fetch_pdf(u) for u in pending_pdfs))


def _auth_required_result(url: str, depth: int) -> PageResult:
    return PageResult(
        url, "", "", "", None, [], [], depth=depth,
        error=f"{AUTH_REQUIRED}: la página exige iniciar sesión (Identity corporativo)",
        auth_required=True,
    )


async def crawl_market(
    market_code: str,
    start_urls: list[str],
    allowlist: list[str],
    run_id: int,
    allow_credentials: bool = True,
    strict_public: bool = False,
    allow_saved_session: bool = True,
    allow_login: bool = True,
):
    """Generador asíncrono que produce PageResult por cada página visitada.

    allow_credentials=False (escaneos de URLs externas confirmadas a mano): no se
    rellenan formularios ni se envían credenciales de servicio. allow_saved_session
    sigue permitiendo usar la sesión iniciada a mano (cookies), porque las cookies
    solo viajan a sus propios dominios y es lo que permite auditar Veeva/Kaltura.
    allow_login=False («continuar sin logueo»): no usa la sesión guardada ni
    intenta ningún login; solo recorre el contenido público.
    strict_public=True: cada petición exige IP pública.

    Recorrido en anchura, determinista: los enlaces de cada página se ordenan
    antes de encolarse. MAX_PAGES_PER_SCAN=0 y MAX_DEPTH=0 significan «sin límite».
    """
    evidence_dir = f"data/evidence/run_{run_id}"
    os.makedirs(evidence_dir, exist_ok=True)

    max_pages = int(_setting("max_pages_per_scan", 0) or 0)   # 0 = sin límite
    max_depth = int(_setting("max_depth", 0) or 0)            # 0 = sin límite

    visited: set[str] = set()
    queued: set[str] = set()
    queue: list[tuple[str, int]] = []
    for start in sorted({u for u in start_urls if is_allowed_url(u, allowlist)}):
        normalized = _normalize(start)
        if normalized not in queued:
            queued.add(normalized)
            queue.append((start, 0))
    if not queue:
        raise ValueError(f"El mercado {market_code} no tiene URLs iniciales dentro de domain_allowlist")

    pages_done = 0
    consecutive_auth_pages = 0
    total_auth_pages = 0
    max_consecutive_auth = int(_setting("crawler_max_auth_pages", 0) or 0)   # 0 = nunca se detiene
    max_total_auth = int(_setting("crawler_max_auth_total", 0) or 0)         # 0 = sin tope

    can_login = bool(allow_login and allow_credentials and has_generic_login_credentials())
    has_kaltura_ks = bool(allow_login and allow_saved_session and ks_available())
    has_saved_session = bool(
        allow_login and allow_saved_session and (saved_session_files() or has_kaltura_ks)
    )
    if has_kaltura_ks:
        log.info("Token KS de Kaltura disponible; se reutilizará la sesión de KMC")
    allowed_login_hosts = login_hosts() if (allow_login and allow_credentials) else set()
    logged_in_origins: set[str] = set()
    # Una sola vez por escaneo se intenta el login con credenciales; si falla, no se repite
    login_attempted = False
    login_succeeded = False
    # El Identity solo se permite en las peticiones si hay una forma de autenticarse
    allow_identity_requests = bool(can_login or has_saved_session)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.headless)
        contexts: dict = {}
        pages: dict = {}

        async def page_for(url: str) -> Page:
            """Un contexto de navegador por origen (sesión y credenciales separadas)."""
            parsed = urlparse(url)
            origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            if origin not in pages:
                context_options = {
                    "viewport": {"width": 1366, "height": 900},
                }
                ua = browser_user_agent()
                if ua:
                    context_options["user_agent"] = ua
                # Sesión iniciada por una persona (scripts/save_session.py), si existe.
                # Se aplica a cualquier host autorizado: las cookies del Identity son de otro
                # dominio, así que cargarla en todos los contextos es lo que hace falta.
                session_state = load_saved_session() if (allow_login and allow_saved_session) else None
                if session_state:
                    try:
                        context_options["storage_state"] = session_state
                        log.info(
                            "Usando sesión guardada (%s cookies de: %s)",
                            len(session_state["cookies"]),
                            ", ".join(os.path.basename(p) for p in saved_session_files()),
                        )
                    except Exception:
                        pass
                context = await browser.new_context(**context_options)
                # KMC guarda el KS en sessionStorage, no en cookies. Se inyecta
                # solo en contextos de Kaltura para no exponer el token a otros
                # dominios (AzMed, Veeva, sitios de marca).
                is_kaltura_origin = is_kaltura_host(parsed.hostname or "")
                kaltura_script = (
                    kaltura_init_script()
                    if (allow_login and allow_saved_session and is_kaltura_origin)
                    else None
                )
                if kaltura_script:
                    try:
                        await context.add_init_script(kaltura_script)
                    except Exception:
                        pass
                allowed_hosts = (
                    list(dict.fromkeys([*allowlist, *auth_provider_hosts()]))
                    if allow_identity_requests else allowlist
                )
                await context.route("**/*", lambda route: _guard_request(route, allowed_hosts, strict_public))
                contexts[origin] = context
                pages[origin] = await context.new_page()
            return pages[origin]

        try:
            while queue and (max_pages == 0 or pages_done < max_pages):
                url, depth = queue.pop(0)
                normalized_url = _normalize(url)
                if normalized_url in visited:
                    continue
                visited.add(normalized_url)
                page = await page_for(url)

                # Captura de imágenes y PDFs con la sesión del navegador (evita los 403)
                captured_images: dict[str, bytes] = {}
                captured_pdfs: dict[str, bytes] = {}
                capturer = _make_response_capturer(captured_images, captured_pdfs, allowlist)
                page.on("response", capturer)

                try:
                    await _goto_with_retry(page, url)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass

                    # KMC necesita el handoff del KS en sessionStorage para
                    # recuperar la sesión aunque no haya cookies.
                    if (
                        allow_login
                        and allow_saved_session
                        and is_kaltura_host(page.url)
                    ):
                        await prime_kaltura_page(page)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass

                    # --- Redirección al Identity corporativo ---

                    if _on_identity_host(page):
                        if has_saved_session and not login_succeeded:
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=settings.page_timeout_ms)
                                await page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                pass
                        if _on_identity_host(page):
                            if can_login and not login_attempted:
                                login_attempted = True
                                try:
                                    login_succeeded = await login_to_identity(page, allowlist)
                                except Exception as login_exc:
                                    log.warning("Login fallido en %s: %s", url, login_exc)
                                    login_succeeded = False
                                if login_succeeded:
                                    log.info("Sesión iniciada en el Identity; se continúa el rastreo")
                                    try:
                                        await _goto_with_retry(page, url)
                                        await page.wait_for_load_state("networkidle", timeout=8000)
                                    except Exception:
                                        pass
                            elif can_login and login_succeeded:
                                try:
                                    if await login_to_identity(page, allowlist):
                                        await _goto_with_retry(page, url)
                                except Exception as relogin_exc:
                                    log.warning("Reintento de login fallido: %s", relogin_exc)

                    if _on_identity_host(page):
                        consecutive_auth_pages += 1
                        total_auth_pages += 1
                        log.info("Página protegida (requiere autenticación): %s", url)
                        yield _auth_required_result(url, depth)

                        stop_reason = None
                        if max_consecutive_auth and consecutive_auth_pages >= max_consecutive_auth:
                            stop_reason = f"{consecutive_auth_pages} páginas seguidas exigen iniciar sesión"
                        elif max_total_auth and total_auth_pages >= max_total_auth:
                            stop_reason = f"{total_auth_pages} páginas protegidas en total"
                        if stop_reason:
                            log.warning("Se detiene el escaneo: %s", stop_reason)
                            yield PageResult(
                                url, "", "", "", None, [], [], depth=depth,
                                error=f"{AUTH_STOPPED}: {stop_reason}. No hay una sesión autenticada válida.",
                                auth_required=True,
                            )
                            return
                        await asyncio.sleep(settings.request_delay_seconds)
                        continue
                    consecutive_auth_pages = 0


                    # Si una redirección nos sacó de la lista autorizada, no se analiza
                    if not is_allowed_url(page.url, allowlist):
                        raise RuntimeError(f"Redirección fuera del ámbito autorizado: {page.url}")

                    # Login genérico del propio sitio (formulario en el host de CRAWLER_LOGIN_URL):
                    # solo en los hosts configurados y una vez por origen.
                    page_origin = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}".lower()
                    if (page_origin not in logged_in_origins
                            and may_send_login(page.url, allowlist, allow_credentials, allowed_login_hosts)):
                        if await _login_on_site(page, allowlist):
                            logged_in_origins.add(page_origin)

                    await _try_accept_cookies(page)
                    await _auto_scroll(page)
                    image_urls = await _collect_image_urls(page, url, allowlist)
                    await _fetch_missing_resources(
                        page, url, allowlist, captured_images, captured_pdfs, strict_public,
                        image_urls=image_urls,
                    )

                    html = await page.content()
                    visible_text = await page.evaluate("document.body ? document.body.innerText : ''")
                    footer_text = await page.evaluate(
                        """() => {
                            const f = document.querySelector('footer');
                            return f ? f.innerText : '';
                        }"""
                    )

                    shot = f"{evidence_dir}/page_{pages_done + 1}.jpg"
                    try:
                        await page.screenshot(path=shot, full_page=True, type="jpeg", quality=55)
                    except Exception:
                        shot = None

                    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    links, pdfs = [], []
                    for href in hrefs:
                        absolute = urljoin(url, href)
                        if not absolute.startswith("http"):
                            continue
                        path_only = absolute.lower().split("?")[0]
                        if path_only.endswith(".pdf"):
                            if is_allowed_url(absolute, allowlist):
                                pdfs.append(absolute)
                            continue
                        if any(path_only.endswith(ext) for ext in SKIP_EXTENSIONS):
                            continue
                        if _is_malformed_link(absolute):
                            continue  # enlace mal escrito en el sitio (falta https://)
                        if is_allowed_url(absolute, allowlist):
                            links.append(absolute)

                    # Orden determinista y sin duplicados: dos escaneos recorren lo mismo
                    unique_links = sorted({_normalize(link): link for link in links}.items())
                    pages_done += 1
                    yield PageResult(
                        url=url,
                        html=html,
                        visible_text=visible_text,
                        footer_text=footer_text,
                        screenshot_path=shot,
                        links=[original for _, original in unique_links],
                        pdf_links=sorted(set(pdfs)),
                        depth=depth,
                        image_bytes=captured_images,
                        pdf_bytes=captured_pdfs,
                        image_urls=image_urls,
                    )


                    if max_depth == 0 or depth < max_depth:
                        for normalized_link, original in unique_links:
                            if normalized_link not in visited and normalized_link not in queued:
                                queued.add(normalized_link)
                                queue.append((original, depth + 1))

                except Exception as exc:
                    log.warning("Error en %s: %s", url, exc)
                    # Si el navegador se cerró (o lo mataron), no tiene sentido
                    # seguir intentando el resto de la cola: se generaban decenas
                    # de errores y el proceso quedaba colgado.
                    if not browser.is_connected() or "Target page, context or browser has been closed" in str(exc):
                        log.warning("El navegador se cerró; se detiene el escaneo para no dejar procesos huérfanos")
                        return
                    # html vacío: el pipeline cuenta esta página como fallida
                    yield PageResult(url, "", "", "", None, [], [], depth=depth, error=str(exc)[:300])
                finally:
                    try:
                        page.remove_listener("response", capturer)
                    except Exception:
                        pass

                await asyncio.sleep(settings.request_delay_seconds)
        finally:
            # Cada cierre se protege por separado: si el usuario cerró Chromium,
            # context.close() lanza TargetClosedError y antes impedía que se
            # ejecutara browser.close(), dejando el driver vivo.
            for context in contexts.values():
                try:
                    await asyncio.wait_for(context.close(), timeout=10)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(browser.close(), timeout=15)
            except Exception:
                pass
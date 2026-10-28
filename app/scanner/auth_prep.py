"""Captura guiada de sesión para AzMed / Veeva / Kaltura.

Dos modos:

* ``--guided`` (lo usa el dashboard): abre UNA ventana del navegador elegido y
  recorre las plataformas en orden. Cuando detecta que la sesión de una
  plataforma ya está autenticada, cierra esa pestaña y pasa a la siguiente. Al
  terminar, guarda ``data/auth_state.json`` y cierra el navegador solo.
* Sin ``--guided`` (lo usa ``scripts/save_session.py``): abre todas las URLs en
  pestañas y guarda la sesión cada pocos segundos hasta que el usuario cierre
  la ventana.

El proceso escribe su avance en ``data/auth_prep_status.json`` y acepta
órdenes de ``data/auth_prep_control.json`` (omitir paso / detener). Así el
dashboard puede mostrar el progreso y cerrar el navegador sin dejar procesos
huérfanos.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app.config import get_settings
from app.scanner.browser_choice import (
    browser_label,
    launch_browser,
    resolve_browser,
    sanitize_browser,
)
from app.scanner.kaltura_session import (
    extract_ks_from_request,
    init_script as kaltura_init_script,
    ks_available,
    ks_from_page,
    prime_page,
    save_ks,
)
from app.scanner.session_state import (
    PLATFORM_ORDER,
    PLATFORM_RULES,
    PLATFORM_URLS,
    SESSION_FILES,
    platform_covered,
    platform_details,
)

BASE_DIR = Path(__file__).resolve().parents[2]
SESSION_FILE = SESSION_FILES[0]
LOG_FILE = BASE_DIR / "data" / "auth_prep.log"
STATUS_FILE = BASE_DIR / "data" / "auth_prep_status.json"
PID_FILE = BASE_DIR / "data" / "auth_prep.pid"
CONTROL_FILE = BASE_DIR / "data" / "auth_prep_control.json"
LOCK_FILE = BASE_DIR / "data" / "auth_prep.lock"

SAVE_EVERY_SECONDS = 5
STEP_POLL_SECONDS = 2
MAX_LOG_BYTES = 512_000

DEFAULT_URLS = [
    "https://www.azmed.cr/",
    "https://login.veevavault.com/auth/login?retURL=https%3A%2F%2Fastrazeneca-promomats.veevavault.com%2Fui%2F",
    "https://kmc.kaltura.com/index.php/kmcng/login",
]

# Hosts de SSO que pueden aparecer durante el login de AzMed.
AZMED_SSO_HOSTS = (
    "identity.astrazeneca.com",
    "login.astrazeneca.com",
    "sso.astrazeneca.com",
    "login.microsoftonline.com",
    "login.live.com",
    "b2clogin.com",
)

PLATFORM_CONFIG = {
    "azmed": {
        "label": "AzMed / Identity",
        "url": PLATFORM_URLS["azmed"],
        "login_hosts": AZMED_SSO_HOSTS,
        "login_path_tokens": ("/login", "/signin", "/auth", "/sso", "/mfa"),
    },
    "veeva": {
        "label": "Veeva Vault",
        "url": PLATFORM_URLS["veeva"],
        "login_hosts": ("login.veevavault.com",),
        "login_path_tokens": ("/auth/login", "/login"),
    },
    "kaltura": {
        "label": "Kaltura",
        "url": PLATFORM_URLS["kaltura"],
        "login_hosts": (),
        "login_path_tokens": ("/login", "/signin"),
    },
}

_lock_handle = None


# ---------------------------------------------------------------------------
# Utilidades de estado / log
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            rotated = LOG_FILE.with_suffix(".log.1")
            try:
                if rotated.exists():
                    rotated.unlink()
                LOG_FILE.replace(rotated)
            except Exception:
                pass
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _initial_steps() -> list[dict]:
    return [
        {
            "id": key,
            "label": PLATFORM_CONFIG[key]["label"],
            "url": PLATFORM_CONFIG[key]["url"],
            "status": "pending",
            "detail": "",
        }
        for key in PLATFORM_ORDER
    ]


def _update_status(**changes) -> dict:
    status = _read_json(STATUS_FILE, {})
    if not isinstance(status, dict):
        status = {}
    status.update(changes)
    status["updated_at"] = _now_iso()
    _write_json(STATUS_FILE, status)
    return status


def _update_step(step_id: str, step_status: str, detail: str = "") -> None:
    status = _read_json(STATUS_FILE, {})
    if not isinstance(status, dict):
        status = {}
    steps = status.get("steps")
    if not isinstance(steps, list) or not steps:
        steps = _initial_steps()
    for step in steps:
        if step.get("id") == step_id:
            step["status"] = step_status
            step["detail"] = detail
            break
    status["steps"] = steps
    status["current"] = step_id if step_status == "active" else None
    status["updated_at"] = _now_iso()
    _write_json(STATUS_FILE, status)


def _take_control() -> dict:
    data = _read_json(CONTROL_FILE, {})
    try:
        CONTROL_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Bloqueo de instancia única
# ---------------------------------------------------------------------------
def _acquire_lock() -> bool:
    """Impide que dos clics abran dos navegadores que compitan por el mismo archivo."""
    global _lock_handle
    if _lock_handle is not None:
        return True
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.write("x")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def _release_lock() -> None:
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            _lock_handle.seek(0)
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _lock_handle.close()
    except Exception:
        pass
    _lock_handle = None


def _write_pid() -> None:
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sesión de Playwright
# ---------------------------------------------------------------------------
def _load_existing_state() -> dict | None:
    """Combina las sesiones guardadas para no perder cookies de otras plataformas."""
    cookies: dict[tuple, dict] = {}
    origins: dict[str, dict] = {}
    for path in reversed(SESSION_FILES):
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
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
    if not cookies and not origins:
        return None
    return {"cookies": list(cookies.values()), "origins": list(origins.values())}


async def _save_state(context) -> int:
    state = await context.storage_state()
    tmp = SESSION_FILE.with_suffix(".json.tmp")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except Exception:
        pass
    return len(state.get("cookies", []))


async def _safe_close(target, timeout: float = 10.0) -> None:
    """Cierra sin colgar el proceso si Playwright ya perdió el navegador."""
    try:
        await asyncio.wait_for(target.close(), timeout=timeout)
    except Exception:
        pass


async def _launch(p, headless: bool, browser: str = "auto"):
    """Abre el navegador elegido y devuelve ``(browser, context, id_real)``.

    ``auto`` usa Chrome o Edge si están instalados; si no, el Chromium de
    Playwright. El token KS de Kaltura se inyecta en cada contexto para que
    KMC recupere la sesión sin depender de cookies.
    """
    settings = get_settings()
    browser_instance, actual = await launch_browser(p, browser, headless)
    context_options = {
        "viewport": {"width": 1400, "height": 900},
        "user_agent": settings.user_agent,
        "ignore_https_errors": True,
    }
    existing = _load_existing_state()
    if existing:
        context_options["storage_state"] = existing
    try:
        context = await browser_instance.new_context(**context_options)
    except Exception:
        await _safe_close(browser_instance)
        raise
    return browser_instance, context, actual


def _host_matches(host: str, candidate: str) -> bool:
    host = (host or "").lower().lstrip(".")
    candidate = (candidate or "").lower().lstrip(".")
    return host == candidate or host.endswith(f".{candidate}")


def _is_kaltura_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return _host_matches(host, "kaltura.com")


async def _add_kaltura_init_script(page) -> None:
    """Inyecta el KS solo en la página de KMC, antes de cargarla."""
    script = kaltura_init_script()
    if not script:
        return
    try:
        await page.add_init_script(script)
        _log("Token KS de Kaltura cargado para reutilizar la sesión.")
    except Exception as exc:
        _log(f"No se pudo inyectar el token de Kaltura: {exc}")


def _capture_kaltura_request(request) -> None:
    """Guarda el KS que KMC envía en sus peticiones (sin registrarlo)."""
    try:
        ks = extract_ks_from_request(
            request.url,
            request.post_data,
            dict(request.headers),
        )
        if ks:
            save_ks(ks)
    except Exception:
        pass


async def _capture_ks_from_page(page) -> str | None:
    """Lee y guarda el KS de la página de KMC si está disponible."""
    try:
        if not _is_kaltura_url(page.url):
            return None
    except Exception:
        return None
    ks = await ks_from_page(page)
    if ks:
        save_ks(ks)
    return ks


async def _capture_ks_from_context(context) -> bool:
    for page in list(context.pages):
        if await _capture_ks_from_page(page):
            return True
    return False


def _is_login_url(url: str, platform_id: str) -> bool:
    config = PLATFORM_CONFIG[platform_id]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if any(_host_matches(host, candidate) for candidate in config.get("login_hosts", ())):
        return True
    if any(token in path for token in config.get("login_path_tokens", ())):
        return True
    return False


async def _platform_ready(page, context, platform_id: str) -> tuple[bool, bool, bool, str]:
    """Devuelve (listo, cubierto, en_pantalla_login, url_actual)."""
    try:
        url = page.url
    except Exception:
        return False, False, False, ""
    try:
        cookies = await context.cookies()
    except Exception:
        return False, False, False, url
    covered = platform_covered(cookies, platform_id)
    on_login = _is_login_url(url, platform_id)
    if platform_id == "kaltura" and not on_login:
        # KMC no deja cookie de sesión: el KS vive en sessionStorage. Se
        # captura de la página o de las peticiones y se reutiliza después.
        ks = await _capture_ks_from_page(page)
        if ks or ks_available():
            covered = True
    return bool(covered and not on_login), covered, on_login, url


async def _guided_step(browser, context, platform_id: str, deadline: float) -> str:
    """Abre una plataforma y espera a que la sesión quede autenticada.

    Devuelve: done | skipped | stopped | timeout | closed | error.
    """
    config = PLATFORM_CONFIG[platform_id]
    page = await context.new_page()
    if platform_id == "kaltura":
        await _add_kaltura_init_script(page)
        page.on("request", _capture_kaltura_request)
    try:
        try:
            await page.goto(config["url"], wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            _log(f"Abierta {config['label']}: {config['url']}")
        except Exception as exc:
            _log(f"Advertencia al abrir {config['label']}: {exc}")
            # La ventana sigue abierta: el usuario puede navegar a mano.

        next_prime_at = 0.0
        if platform_id == "kaltura":
            await prime_page(page)
            next_prime_at = time.monotonic() + 3.0

        stable = 0
        while time.monotonic() < deadline:
            control = _take_control()
            if control.get("stop"):
                return "stopped"
            if control.get("skip"):
                return "skipped"

            if page.is_closed() or not browser.is_connected():
                return "closed"

            if platform_id == "kaltura" and next_prime_at and time.monotonic() >= next_prime_at:
                await prime_page(page)
                next_prime_at = 0.0

            ready, covered, on_login, url = await _platform_ready(page, context, platform_id)
            if ready:
                stable += 1
                if stable >= 2:
                    if platform_id == "kaltura":
                        await _capture_ks_from_page(page)
                    _log(f"Sesión detectada en {config['label']} ({url})")
                    return "done"
            else:
                stable = 0
                if on_login and covered:
                    # Cookie presente pero seguimos en la pantalla de login:
                    # probablemente el usuario aún no completó el SSO.
                    pass

            await asyncio.sleep(STEP_POLL_SECONDS)

        _log(f"Tiempo agotado esperando el login de {config['label']}")
        return "timeout"
    except Exception as exc:
        _log(f"Error en el paso {config['label']}: {exc}")
        return "error"
    finally:
        try:
            if not page.is_closed():
                await _safe_close(page, timeout=5.0)
        except Exception:
            pass


async def prepare_guided_session(headless: bool = False, browser: str = "auto") -> dict:
    """Login secuencial AzMed -> Veeva -> Kaltura con cierre automático."""
    settings = get_settings()
    if not _acquire_lock():
        _log("Ya hay una ventana de login abierta. No se abre otra.")
        return {"ok": False, "error": "already_running"}

    requested_browser = sanitize_browser(browser)
    expected_browser = resolve_browser(requested_browser)
    _write_pid()
    started_at = _now_iso()
    _write_json(STATUS_FILE, {
        "running": True,
        "finished": False,
        "mode": "guided",
        "started_at": started_at,
        "updated_at": started_at,
        "current": None,
        "message": f"Abriendo {browser_label(expected_browser)}…",
        "cookies": 0,
        "steps": _initial_steps(),
        "error": None,
        "pid": os.getpid(),
        "browser": expected_browser,
        "browser_label": browser_label(expected_browser),
    })
    _log(f"Inicio de sesión guiado con {browser_label(expected_browser)}: AzMed -> Veeva -> Kaltura")

    final_message = "Sesión finalizada."
    final_error = None
    saved_cookies = 0
    final_platforms: dict[str, bool] = {}
    completed = False
    actual_browser = expected_browser

    try:
        async with async_playwright() as p:
            browser_instance, context, actual_browser = await _launch(
                p, headless=headless, browser=requested_browser
            )
            _update_status(
                browser=actual_browser,
                browser_label=browser_label(actual_browser),
                message=f"Abriendo {browser_label(actual_browser)}…",
            )
            try:
                overall_deadline = time.monotonic() + max(60, settings.auth_prep_max_seconds)
                for index, platform_id in enumerate(PLATFORM_ORDER):
                    config = PLATFORM_CONFIG[platform_id]
                    if time.monotonic() >= overall_deadline:
                        _update_step(platform_id, "skipped", "Tiempo total agotado")
                        continue

                    _update_step(platform_id, "active")
                    _update_status(
                        message=f"Paso {index + 1} de {len(PLATFORM_ORDER)}: inicia sesión en {config['label']}.",
                        current=platform_id,
                    )
                    _log(f"Paso {index + 1}/{len(PLATFORM_ORDER)}: {config['label']}")

                    step_deadline = min(
                        time.monotonic() + max(30, settings.auth_step_timeout_seconds),
                        overall_deadline,
                    )
                    outcome = await _guided_step(
                        browser_instance, context, platform_id, step_deadline
                    )

                    try:
                        await _capture_ks_from_context(context)
                        saved_cookies = await _save_state(context)
                        final_platforms = {
                            key: bool(details["covered"])
                            for key, details in platform_details(
                                (await context.cookies()),
                                kaltura_ks=ks_available(),
                            ).items()
                        }
                        _update_status(cookies=saved_cookies)
                    except Exception as exc:
                        _log(f"No se pudo guardar la sesión tras {config['label']}: {exc}")

                    if outcome == "done":
                        _update_step(platform_id, "done", "Sesión detectada")
                    elif outcome == "skipped":
                        _update_step(platform_id, "skipped", "Omitido por el usuario")
                    elif outcome == "timeout":
                        _update_step(platform_id, "skipped", "No se detectó el inicio de sesión a tiempo")
                    elif outcome == "closed":
                        _update_step(platform_id, "skipped", "Ventana cerrada")
                        final_message = "Ventana cerrada. Se guardó la sesión de las plataformas completadas."
                        break
                    elif outcome == "stopped":
                        _update_step(platform_id, "skipped", "Detenido por el usuario")
                        final_message = "Sesión detenida por el usuario."
                        break
                    else:
                        _update_step(platform_id, "error", "Error al abrir la plataforma")
                        final_error = final_error or f"Error en {config['label']}"

                try:
                    await _capture_ks_from_context(context)
                    saved_cookies = await _save_state(context)
                    final_platforms = {
                        key: bool(details["covered"])
                        for key, details in platform_details(
                            (await context.cookies()),
                            kaltura_ks=ks_available(),
                        ).items()
                    }
                except Exception as exc:
                    final_error = final_error or str(exc)

                completed = all(final_platforms.get(key, False) for key in PLATFORM_ORDER)
                if final_message == "Sesión finalizada.":
                    if completed:
                        final_message = "Sesión guardada. AzMed, Veeva y Kaltura están autenticados."
                    else:
                        missing = [
                            PLATFORM_CONFIG[key]["label"]
                            for key in PLATFORM_ORDER
                            if not final_platforms.get(key, False)
                        ]
                        final_message = (
                            "Sesión guardada parcialmente. Sin detectar: "
                            + ", ".join(missing)
                            + ". Puedes continuar; el escáner auditará lo que permita cada sesión."
                        )
            finally:
                await _safe_close(context)
                await _safe_close(browser_instance)
    except Exception as exc:
        final_error = str(exc)
        final_message = f"No se pudo completar el login: {exc}"
        _log(final_message)
    finally:
        _update_status(
            running=False,
            finished=True,
            current=None,
            message=final_message,
            error=final_error,
            cookies=saved_cookies,
            platforms=final_platforms,
            browser=actual_browser,
            browser_label=browser_label(actual_browser),
            kaltura_ks=ks_available(),
        )
        _remove_pid()
        _release_lock()
        _log(f"Fin del login guiado: {final_message}")

    return {
        "ok": final_error is None,
        "path": str(SESSION_FILE),
        "cookies": saved_cookies,
        "platforms": final_platforms,
        "kaltura_ks": ks_available(),
        "browser": actual_browser,
        "browser_label": browser_label(actual_browser),
        "message": final_message,
        "error": final_error,
    }


# ---------------------------------------------------------------------------
# Modo clásico (scripts/save_session.py)
# ---------------------------------------------------------------------------
def session_urls(cli_urls: list[str] | None = None) -> list[str]:
    """URLs que se abren para el login. Prioridad: CLI > AUTH_PREP_URLS > lista por defecto."""
    if cli_urls:
        return [u.strip() for u in cli_urls if u and u.strip()]
    raw = os.getenv("AUTH_PREP_URLS", "").strip()
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return list(DEFAULT_URLS)


async def prepare_session(
    cli_urls: list[str] | None = None,
    headless: bool = False,
    browser: str = "auto",
) -> dict:
    """Modo clásico: todas las URLs en pestañas hasta que el usuario cierre el navegador."""
    if not _acquire_lock():
        _log("Ya hay una ventana de login abierta. No se abre otra.")
        return {"ok": False, "error": "already_running"}

    urls = session_urls(cli_urls)
    requested_browser = sanitize_browser(browser)
    expected_browser = resolve_browser(requested_browser)
    _log(f"Abriendo {browser_label(expected_browser)} para iniciar sesión en:")
    for url in urls:
        _log(f"  - {url}")
    _log(
        "Inicia sesión con SSO/MFA en cada pestaña. La sesión se guarda sola cada "
        f"{SAVE_EVERY_SECONDS}s. Cierra el navegador cuando termines."
    )

    saved_cookies = 0
    last_logged_count = -1
    last_error = ""
    actual_browser = expected_browser
    try:
        async with async_playwright() as p:
            browser_instance, context, actual_browser = await _launch(
                p, headless=headless, browser=requested_browser
            )
            try:
                first = await context.new_page()
                for index, url in enumerate(urls):
                    page = first if index == 0 else await context.new_page()
                    if _is_kaltura_url(url):
                        await _add_kaltura_init_script(page)
                        page.on("request", _capture_kaltura_request)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        _log(f"Abierta: {url}")
                        if _is_kaltura_url(url):
                            await prime_page(page)
                    except Exception as exc:
                        _log(f"Advertencia al abrir {url}: {exc}")

                try:
                    await _capture_ks_from_context(context)
                    saved_cookies = await _save_state(context)
                    _log(f"Sesión inicial guardada ({saved_cookies} cookies) en {SESSION_FILE}")
                except Exception as exc:
                    last_error = str(exc)

                no_pages = 0
                overall_deadline = time.monotonic() + max(60, get_settings().auth_prep_max_seconds)
                while time.monotonic() < overall_deadline:
                    control = _take_control()
                    if control.get("stop"):
                        _log("Sesión detenida por el usuario.")
                        break
                    if not browser_instance.is_connected():
                        break
                    if not context.pages:
                        no_pages += 1
                        if no_pages >= 2:
                            break
                    else:
                        no_pages = 0
                    try:
                        await _capture_ks_from_context(context)
                        saved_cookies = await _save_state(context)
                        if saved_cookies != last_logged_count:
                            _log(f"Sesión guardada ({saved_cookies} cookies)")
                            last_logged_count = saved_cookies
                    except Exception as exc:
                        # La ventana se cerró: se conserva el último guardado correcto
                        last_error = str(exc)
                        break
                    await asyncio.sleep(SAVE_EVERY_SECONDS)
            finally:
                await _safe_close(context)
                await _safe_close(browser_instance)

        if saved_cookies == 0 and SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                saved_cookies = len(data.get("cookies", []))
            except Exception:
                pass

        _log(
            f"{browser_label(actual_browser)} cerrado. Sesión final: "
            f"{saved_cookies} cookies en {SESSION_FILE}"
        )
        if last_error:
            _log(f"Nota: {last_error}")
        return {
            "ok": True,
            "path": str(SESSION_FILE),
            "cookies": saved_cookies,
            "kaltura_ks": ks_available(),
            "browser": actual_browser,
            "browser_label": browser_label(actual_browser),
        }
    finally:
        _release_lock()


# ---------------------------------------------------------------------------
# Entradas síncronas
# ---------------------------------------------------------------------------
def _set_windows_event_loop_policy() -> None:
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass


def run_sync(
    cli_urls: list[str] | None = None,
    headless: bool = False,
    browser: str = "auto",
) -> dict:
    _set_windows_event_loop_policy()
    return asyncio.run(
        prepare_session(cli_urls=cli_urls, headless=headless, browser=browser)
    )


def run_guided_sync(headless: bool = False, browser: str = "auto") -> dict:
    _set_windows_event_loop_policy()
    return asyncio.run(prepare_guided_session(headless=headless, browser=browser))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Abre un navegador para iniciar sesión a mano y guardar data/auth_state.json"
    )
    parser.add_argument("--url", action="append", dest="urls",
                        help="URL de login; puede repetirse. Por defecto: AzMed, Veeva Vault y Kaltura.")
    parser.add_argument("--guided", action="store_true",
                        help="Login secuencial con detección automática y cierre del navegador.")
    parser.add_argument(
        "--browser",
        choices=("auto", "chromium", "chrome", "edge"),
        default="auto",
        help="Navegador: auto (Chrome/Edge si están instalados), chromium, chrome o edge.",
    )
    parser.add_argument("--headless", action="store_true", help="No mostrar la ventana (no recomendado)")
    args = parser.parse_args()

    if args.guided:
        result = run_guided_sync(args.headless, args.browser)
    else:
        result = run_sync(args.urls, args.headless, args.browser)
    if not result.get("ok"):
        raise SystemExit(1)

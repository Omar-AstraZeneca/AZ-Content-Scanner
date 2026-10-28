"""API de sesión para contenido protegido (AzMed / Veeva / Kaltura).

El login se ejecuta en un proceso aparte porque Playwright necesita abrir una
ventana de navegador visible (Chrome, Edge o Chromium). Este router solo lanza,
consulta y detiene ese proceso; nunca maneja credenciales ni cookies desde el
navegador del dashboard.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import get_settings
from app.scanner.browser_choice import (
    available_browsers,
    browser_label,
    resolve_browser,
    sanitize_browser,
)
from app.scanner.session_state import (
    PLATFORM_ORDER,
    PLATFORM_RULES,
    PLATFORM_URLS,
    SESSION_FILES,
    session_info,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

BASE_DIR = Path(__file__).resolve().parents[2]
SESSION_FILE = SESSION_FILES[0]
LOG_FILE = BASE_DIR / "data" / "auth_prep.log"
STATUS_FILE = BASE_DIR / "data" / "auth_prep_status.json"
PID_FILE = BASE_DIR / "data" / "auth_prep.pid"
API_PID_FILE = BASE_DIR / "data" / "auth_prep_api.pid"
CONTROL_FILE = BASE_DIR / "data" / "auth_prep_control.json"

# Serializa las peticiones de /prepare-session para que dos clics simultáneos
# no lancen dos procesos de login compitiendo por el mismo archivo de sesión.
_prepare_lock = threading.Lock()


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


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_process_tree(pid: int) -> bool:
    """Mata el proceso de login y sus hijos (el navegador)."""
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _initial_steps() -> list[dict]:
    return [
        {
            "id": key,
            "label": PLATFORM_RULES[key]["label"],
            "url": PLATFORM_URLS[key],
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


def _process_status() -> dict:
    """Estado del proceso de login, corrigiendo procesos que ya murieron."""
    status = _read_json(STATUS_FILE, {})
    if not isinstance(status, dict) or not status:
        return {
            "running": False,
            "finished": False,
            "mode": "guided",
            "steps": _initial_steps(),
            "current": None,
            "message": "Sin proceso de login en curso.",
            "cookies": 0,
            "error": None,
            "browser": None,
            "browser_label": None,
        }

    pid = status.get("pid") or _read_pid_file(PID_FILE) or _read_pid_file(API_PID_FILE)
    running = bool(status.get("running"))
    if running and pid and not _pid_alive(int(pid)):
        # Margen de gracia: el proceso acaba de arrancar y aún no ha escrito su PID.
        updated_at = status.get("updated_at")
        stale = True
        if updated_at:
            try:
                age = (datetime.now() - datetime.fromisoformat(updated_at)).total_seconds()
                stale = age > 15
            except Exception:
                pass
        if stale:
            status.update({
                "running": False,
                "finished": True,
                "current": None,
                "message": "El proceso de login terminó sin cerrar la sesión.",
                "error": status.get("error") or "El proceso de login terminó inesperadamente.",
            })
            _write_json(STATUS_FILE, status)
            running = False

    status["running"] = running
    # Si el proceso ya terminó, los PID guardados son basura y un PID reciclado
    # podría pertenecer a otro programa: se borran para no matarlo por error.
    if not running and status.get("finished"):
        _remove_pid_files()
    status.setdefault("steps", _initial_steps())
    status.setdefault("current", None)
    status.setdefault("cookies", 0)
    status.setdefault("error", None)
    return status


def _api_pid() -> int | None:
    pid = _read_pid_file(API_PID_FILE)
    if pid and _pid_alive(pid):
        return pid
    return None


def _remove_pid_files() -> None:
    for path in (API_PID_FILE, PID_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _session_payload() -> dict:
    info = session_info()
    process = _process_status()
    info["process"] = process
    info["running"] = bool(process.get("running"))
    return info


@router.get("/session-status")
def session_status():
    """Estado combinado: cookies guardadas + proceso de login en curso."""
    return _session_payload()


@router.get("/browsers")
def browsers():
    """Navegadores que se pueden usar para el login en esta máquina.

    ``auto`` prefiere Chrome, luego Edge y, si ninguno está instalado, el
    Chromium incluido por Playwright.
    """
    options = [
        {
            "id": "auto",
            "label": "Automático (Chrome o Edge instalado)",
            "available": True,
        },
        *available_browsers(),
    ]
    return {
        "browsers": options,
        "resolved": resolve_browser("auto"),
        "resolved_label": browser_label(resolve_browser("auto")),
    }


@router.get("/session-log")
def session_log():
    """Últimas líneas del proceso de login (solo para diagnóstico)."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        lines = []
    return {"lines": lines[-40:]}


class PrepareSessionIn(BaseModel):
    mode: str = "guided"
    platforms: list[str] | None = None
    browser: str | None = None


@router.post("/prepare-session")
def prepare_session(body: PrepareSessionIn | None = None):
    """Abre el navegador para iniciar sesión (idempotente y serializado).

    El lock evita que dos clics simultáneos lancen dos procesos de login que
    compitan por el mismo `data/auth_state.json`.
    """
    with _prepare_lock:
        return _prepare_session_locked(body)


def _prepare_session_locked(body: PrepareSessionIn | None = None):
    """Abre el navegador para iniciar sesión de forma guiada y secuencial.

    Es idempotente: si ya hay una ventana de login abierta, no lanza otra.
    """
    current = _process_status()
    if current.get("running"):
        return {
            "ok": True,
            "already_running": True,
            "message": "La ventana de sesión ya está abierta. Termina el login en el navegador.",
            "status": current,
        }

    default_browser = get_settings().auth_browser
    requested_browser = sanitize_browser(
        (body.browser if body and body.browser else default_browser)
    )
    actual_browser = resolve_browser(requested_browser)
    browser_text = browser_label(actual_browser)

    try:
        CONTROL_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Estado inicial para que el dashboard muestre los tres pasos de inmediato.
    _write_json(STATUS_FILE, {
        "running": True,
        "finished": False,
        "mode": "guided",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "current": None,
        "message": f"Abriendo {browser_text}…",
        "cookies": 0,
        "steps": _initial_steps(),
        "error": None,
        "browser": actual_browser,
        "browser_label": browser_text,
    })

    try:
        creationflags = 0
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            # Sin consola: solo debe aparecer el navegador. CREATE_NEW_PROCESS_GROUP
            # permite matar el árbol completo si el dashboard se cierra.
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.scanner.auth_prep",
                "--guided",
                "--browser",
                actual_browser,
            ],
            cwd=str(BASE_DIR),
            creationflags=creationflags,
            shell=False,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
        API_PID_FILE.write_text(str(process.pid), encoding="utf-8")
        _update_status(pid=process.pid)
        return {
            "ok": True,
            "already_running": False,
            "pid": process.pid,
            "browser": actual_browser,
            "browser_label": browser_text,
            "message": f"Se está abriendo {browser_text}. Inicia sesión en cada "
                       "plataforma; la ventana se cerrará sola al detectar el acceso.",
            "status": _process_status(),
        }
    except Exception as exc:
        _update_status(
            running=False,
            finished=True,
            current=None,
            message=f"No se pudo abrir {browser_text}.",
            error=str(exc),
            browser=actual_browser,
            browser_label=browser_text,
        )
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@router.post("/skip-step")
def skip_step():
    """Pide al proceso de login que omita la plataforma actual y pase a la siguiente."""
    _write_json(CONTROL_FILE, {"skip": True})
    return {"ok": True, "message": "Se omitirá el paso actual."}


@router.post("/session-stop")
def session_stop():
    """Detiene el proceso de login y cierra el navegador sin dejar procesos huérfanos."""
    _write_json(CONTROL_FILE, {"stop": True})
    current = _process_status()
    pid = _api_pid() or _read_pid_file(PID_FILE)
    # Solo matar si la API cree que hay un login en curso. Así un PID viejo
    # reciclado por otro programa no se mata por accidente.
    stopped = bool(current.get("running") and pid and _kill_process_tree(int(pid)))
    _remove_pid_files()
    _update_status(
        running=False,
        finished=True,
        current=None,
        message="Sesión detenida por el usuario.",
        error="Detenido por el usuario",
    )
    return {"ok": True, "stopped": stopped}


def stop_auth_prep_process() -> bool:
    """Cierra el proceso de login lanzado por la API (se usa al apagar uvicorn).

    No toca procesos lanzados a mano con scripts/save_session.py: solo el PID
    que la API guardó en `auth_prep_api.pid` y solo si el estado indica que
    sigue en curso.
    """
    status = _read_json(STATUS_FILE, {})
    pid = _read_pid_file(API_PID_FILE)
    if not pid or not isinstance(status, dict) or status.get("finished"):
        _remove_pid_files()
        return False
    killed = _kill_process_tree(pid)
    _remove_pid_files()
    return killed

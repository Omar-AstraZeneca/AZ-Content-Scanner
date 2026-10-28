"""Selección del navegador para el login guiado.

Playwright puede controlar el Chromium incluido o los Chrome/Edge instalados
en el sistema (canales ``chrome`` y ``msedge``). Así el usuario puede iniciar
sesión en el navegador que ya usa, sin depender del Chromium de Playwright.

Safari no se puede automatizar con Playwright y su almacén de cookies está
protegido por el sistema; en macOS se ofrece Chrome, Edge o Chromium.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BROWSER_LABELS = {
    "chromium": "Chromium (incluido)",
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
}
ALLOWED_BROWSERS = ("auto", "chromium", "chrome", "edge")


def _candidates(browser_id: str) -> list[str]:
    paths: list[str] = []
    if sys.platform == "win32":
        if browser_id == "chrome":
            for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(env)
                if base:
                    paths.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        elif browser_id == "edge":
            for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
                base = os.environ.get(env)
                if base:
                    paths.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    elif sys.platform == "darwin":
        if browser_id == "chrome":
            paths.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        elif browser_id == "edge":
            paths.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    else:
        names = (
            ("google-chrome", "google-chrome-stable")
            if browser_id == "chrome"
            else ("microsoft-edge", "microsoft-edge-stable")
        )
        for name in names:
            found = shutil.which(name)
            if found:
                paths.append(found)
    return paths


def _available(browser_id: str) -> bool:
    return any(Path(path).is_file() for path in _candidates(browser_id))


def available_browsers() -> list[dict]:
    """Navegadores que puede usar el login en esta máquina."""
    result = [
        {
            "id": "chromium",
            "label": BROWSER_LABELS["chromium"],
            "available": True,
        }
    ]
    for browser_id in ("chrome", "edge"):
        result.append(
            {
                "id": browser_id,
                "label": BROWSER_LABELS[browser_id],
                "available": _available(browser_id),
            }
        )
    return result


def sanitize_browser(value: str | None) -> str:
    value = (value or "auto").strip().lower()
    return value if value in ALLOWED_BROWSERS else "auto"


def resolve_browser(value: str | None) -> str:
    """Resuelve ``auto`` al primer navegador instalado (Chrome, Edge, Chromium)."""
    value = sanitize_browser(value)
    if value != "auto":
        return value
    for browser_id in ("chrome", "edge"):
        if _available(browser_id):
            return browser_id
    return "chromium"


def browser_label(value: str | None) -> str:
    return BROWSER_LABELS.get(sanitize_browser(value), BROWSER_LABELS["chromium"])


async def launch_browser(p, choice: str | None, headless: bool):
    """Lanza el navegador elegido y devuelve ``(browser, id_real)``.

    Si Chrome/Edge no están instalados o el canal falla, se usa el Chromium
    incluido para no bloquear el login.
    """
    actual = resolve_browser(choice)
    channels = {"chrome": "chrome", "edge": "msedge"}
    if actual in channels:
        try:
            browser = await p.chromium.launch(channel=channels[actual], headless=headless)
            return browser, actual
        except Exception:
            pass
    browser = await p.chromium.launch(headless=headless)
    return browser, "chromium"

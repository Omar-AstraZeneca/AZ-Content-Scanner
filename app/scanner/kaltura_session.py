"""Persistencia del token KS de Kaltura KMC.

KMC no guarda la sesión en cookies: el token KS viaja en
``window.kmcConfig.ks`` y, si el usuario marcó «recordarme», en
``sessionStorage`` bajo ``auth.login.ks``. Playwright no exporta
``sessionStorage`` en ``storage_state``, así que el token se guarda aparte
(``data/kaltura_session.json``) y se inyecta al crear los contextos del
crawler. El valor nunca se registra en logs ni se devuelve por la API.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

BASE_DIR = Path(__file__).resolve().parents[2]
KS_FILE = BASE_DIR / "data" / "kaltura_session.json"
KS_STORAGE_KEY = "auth.login.ks"

# Un KS de Kaltura dura como máximo un día por defecto. Pasado ese plazo se
# pide de nuevo el login en lugar de mostrar una cobertura falsa.
KS_MAX_AGE = timedelta(hours=24)

# El KS es una cadena larga (base64/URL-safe). Se exige un mínimo para no
# confundirlo con valores cortos de configuración.
_KS_MIN_LENGTH = 40
_KS_QUERY_RE = re.compile(r"(?:^|[?&;,\s\"'])ks=([A-Za-z0-9%_+/=.\-]{40,})")
_KS_JSON_RE = re.compile(r"[\"']ks[\"']\s*[:=]\s*[\"']([^\"']{40,})[\"']")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_ks(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) < _KS_MIN_LENGTH or any(ch.isspace() for ch in value):
        return None
    return value


def save_ks(ks: str) -> bool:
    """Guarda el KS con permisos restringidos. Devuelve False si no es válido."""
    value = _valid_ks(ks)
    if not value:
        return False
    try:
        KS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = KS_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"ks": value, "saved_at": _now_iso()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, KS_FILE)
        try:
            os.chmod(KS_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        return False
    return True


def load_ks() -> str | None:
    """Devuelve el KS guardado si sigue vigente; nunca lo expone en logs."""
    try:
        data = json.loads(KS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = _valid_ks(data.get("ks"))
    if not value:
        return None
    saved_at = data.get("saved_at")
    if isinstance(saved_at, str):
        try:
            age = datetime.now() - datetime.fromisoformat(saved_at)
            if age > KS_MAX_AGE:
                return None
        except ValueError:
            pass
    else:
        # Sin fecha explícita, se usa la del archivo para no confiar en un KS
        # indefinidamente.
        try:
            age = datetime.now() - datetime.fromtimestamp(KS_FILE.stat().st_mtime)
            if age > KS_MAX_AGE:
                return None
        except OSError:
            return None
    return value


def ks_available() -> bool:
    return bool(load_ks())


def ks_mtime() -> float:
    try:
        return KS_FILE.stat().st_mtime
    except OSError:
        return 0.0


def clear_ks() -> None:
    try:
        KS_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def extract_ks_from_request(
    url: str,
    post_data: str | None = None,
    headers: dict | None = None,
) -> str | None:
    """Busca un KS en una petición de red sin registrar su valor."""
    for text in (url, post_data or ""):
        if not text:
            continue
        match = _KS_QUERY_RE.search(text) or _KS_JSON_RE.search(text)
        if match:
            return _valid_ks(unquote(match.group(1)))
    for name, value in (headers or {}).items():
        if not value:
            continue
        if name.lower() in {"x-kaltura-session", "x-kaltura-ks", "ks"}:
            found = _valid_ks(value)
            if found:
                return found
        match = _KS_QUERY_RE.search(value) or _KS_JSON_RE.search(value)
        if match:
            found = _valid_ks(unquote(match.group(1)))
            if found:
                return found
    return None


def init_script() -> str | None:
    """Script que inyecta el KS en sessionStorage antes de cargar KMC."""
    ks = load_ks()
    if not ks:
        return None
    payload = json.dumps(ks)
    return (
        "(() => { try {"
        " if (location.hostname === 'kaltura.com' || location.hostname.endsWith('.kaltura.com')) {"
        f" sessionStorage.setItem('{KS_STORAGE_KEY}', {payload});"
        " } } catch (e) {} })();"
    )


async def ks_from_page(page) -> str | None:
    """Lee el KS de la página de KMC (configuración del servidor o sessionStorage)."""
    try:
        value = await page.evaluate(
            "() => { try {"
            " const cfg = window.kmcConfig && typeof window.kmcConfig.ks === 'string'"
            "   ? window.kmcConfig.ks : '';"
            f" const ss = sessionStorage.getItem('{KS_STORAGE_KEY}') || '';"
            " return (cfg || ss || '').trim();"
            " } catch (e) { return ''; } }"
        )
    except Exception:
        return None
    return _valid_ks(value)


async def prime_page(page) -> bool:
    """Intenta que una página de KMC adopte el KS guardado.

    KMC registra un *handoff* de sesión: escucha cambios de
    ``auth.login.ks`` en ``sessionStorage`` y los usa para iniciar sesión.
    El init script ya deja el valor antes de cargar; aquí se emite además un
    ``StorageEvent`` sintético por si la app ya estaba montada. El KS nunca se
    registra en logs.
    """
    ks = load_ks()
    if not ks:
        return False
    try:
        result = await page.evaluate(
            """(ks) => {
                try {
                    sessionStorage.setItem('auth.login.ks', ks);
                    window.dispatchEvent(new StorageEvent('storage', {
                        key: 'auth.login.ks',
                        newValue: ks,
                        storageArea: sessionStorage,
                    }));
                    return true;
                } catch (e) {
                    return false;
                }
            }""",
            ks,
        )
        return bool(result)
    except Exception:
        return False

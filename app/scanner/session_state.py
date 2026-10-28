"""Lectura y validación de la sesión guardada para AzMed / Veeva / Kaltura.

Un archivo `storage_state` de Playwright puede contener muchas cookies que no
significan que haya una sesión autenticada: cookies de Cloudflare (`__cf_bm`),
analítica (`_ga`), balanceadores (`AWSALB`) o el `JSESSIONID` de la propia
página de login. Este módulo centraliza la regla de qué cookie cuenta como
sesión válida para cada plataforma, para que el dashboard y el proceso de login
usen exactamente el mismo criterio.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.scanner.kaltura_session import ks_available, ks_mtime

BASE_DIR = Path(__file__).resolve().parents[2]
SESSION_FILES = [
    BASE_DIR / "data" / "auth_state.json",
    BASE_DIR / "data" / "auth_state_promomats.json",
    BASE_DIR / "data" / "auth_state_veeva.json",
    BASE_DIR / "data" / "auth_state_kaltura.json",
]
SESSION_FILE = SESSION_FILES[0]

# Cookies que nunca identifican una sesión autenticada.
COMMON_IGNORED_COOKIES = {
    "__cf_bm",
    "_ga",
    "_gid",
    "_gat",
    "_gcl_au",
    "_fbp",
    "awsalb",
    "awsalbcors",
    "s_vi",
    "optanonconsent",
    "optanonalertboxclosed",
    "cookielawinfo-checkbox-analytics",
    "cookielawinfo-checkbox-functional",
    "cookielawinfo-checkbox-necessary",
    "cookielawinfo-checkbox-others",
    "cookielawinfo-checkbox-performance",
}

PLATFORM_RULES: dict[str, dict] = {
    "azmed": {
        "label": "AzMed / Identity",
        # Dominios de los portales AzMed/IDAZ. Las cookies del Identity son
        # válidas: es el SSO corporativo el que mantiene la sesión.
        "domains": (
            "azmed.cr",
            "azmed.com.br",
            "azmed.com.mx",
            "azmedical.co",
            "azmedical.pe",
            "idaz.cr",
            "idaz.gt",
            "idaz.hn",
            "idaz.ni",
            "idaz.pa",
            "idaz.sv",
            "idaz.do",
            "myazmed.co.uk",
            "myastrazeneca.co.uk",
        ),
        "identity_domains": (
            "identity.astrazeneca.com",
            "login.astrazeneca.com",
            "sso.astrazeneca.com",
        ),
        "cookie_names": (
            "session",
            "jsessionid",
            "estsauth",
            "estsauthpersistent",
            "azsso",
            "azmed",
            "idaz",
        ),
        # Un JSESSIONID de un portal AzMed es suficiente; en el Identity se
        # exige una cookie de sesión explícita.
        "identity_cookie_names": (
            "session",
            "estsauth",
            "estsauthpersistent",
            "azsso",
        ),
    },
    "veeva": {
        "label": "Veeva Vault",
        "domains": ("veevavault.com",),
        # El JSESSIONID de login.veevavault.com pertenece a la página de login,
        # no a una sesión autenticada del Vault. Otras cookies del host de login
        # (userProfile) sí se crean tras completar el SSO.
        "exclude_cookies": (("login.veevavault.com", "jsessionid"),),
        "cookie_names": (
            "jsessionid",
            "veeva",
            "vault",
            "session",
            "tk",
            "veevauserssosettings",
            "lastactivets",
            "userprofile",
        ),
    },
    "kaltura": {
        "label": "Kaltura",
        "domains": ("kaltura.com",),
        "cookie_names": ("kmc", "ks", "kaltura", "session"),
    },
}

PLATFORM_ORDER = ("azmed", "veeva", "kaltura")


def _platform_urls() -> dict[str, str]:
    settings = get_settings()
    return {
        "azmed": settings.auth_azmed_url.strip() or "https://www.azmed.cr/",
        "veeva": settings.auth_veeva_url.strip(),
        "kaltura": settings.auth_kaltura_url.strip() or "https://kmc.kaltura.com/index.php/kmcng/login",
    }


PLATFORM_URLS = _platform_urls()


def _domain_matches(domain: str, candidate: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    candidate = (candidate or "").lower().lstrip(".")
    return domain == candidate or domain.endswith(f".{candidate}")


def _host_matches(host: str, candidate: str) -> bool:
    return _domain_matches(host, candidate)


def _cookie_is_ignored(cookie: dict) -> bool:
    name = (cookie.get("name") or "").lower()
    if name in COMMON_IGNORED_COOKIES:
        return True
    return not cookie.get("value")


def _cookie_is_excluded(cookie: dict, rule: dict) -> bool:
    """Cookies de una plataforma que no demuestran sesión.

    `exclude_domains` descarta todas las cookies de un host; `exclude_cookies`
    descarta solo una cookie concreta (p. ej. el JSESSIONID de la pantalla de
    login de Veeva, sin afectar a `userProfile` en ese mismo host).
    """
    domain = (cookie.get("domain") or "").lower()
    name = (cookie.get("name") or "").lower()
    if any(_host_matches(domain, candidate) for candidate in rule.get("exclude_domains", ())):
        return True
    return any(
        _host_matches(domain, candidate_domain) and name == candidate_name.lower()
        for candidate_domain, candidate_name in rule.get("exclude_cookies", ())
    )


def platform_covered(
    cookies: list[dict], platform_id: str, kaltura_ks: bool = False
) -> bool:
    """True si alguna cookie (o el token KS de KMC) demuestra sesión.

    Kaltura KMC no guarda la sesión en cookies: usa un token KS en
    ``sessionStorage``. Cuando ``kaltura_ks`` es True, Kaltura se considera
    cubierta aunque las cookies solo incluyan ``__cf_bm``.
    """
    rule = PLATFORM_RULES.get(platform_id)
    if not rule:
        return False
    if platform_id == "kaltura" and kaltura_ks:
        return True

    allowed_names = {name.lower() for name in rule.get("cookie_names", ())}
    identity_names = {name.lower() for name in rule.get("identity_cookie_names", ())}
    identity_domains = tuple(rule.get("identity_domains", ()))

    for cookie in cookies:
        if _cookie_is_ignored(cookie):
            continue
        name = (cookie.get("name") or "").lower()
        domain = (cookie.get("domain") or "").lower()

        if _cookie_is_excluded(cookie, rule):
            continue

        # Cookies del Identity corporativo (SSO de AzMed)
        if identity_domains and any(_host_matches(domain, candidate) for candidate in identity_domains):
            if name in identity_names:
                return True
            continue

        if any(_domain_matches(domain, candidate) for candidate in rule["domains"]):
            if name in allowed_names:
                return True

    return False


def platform_details(
    cookies: list[dict], kaltura_ks: bool = False
) -> dict[str, dict]:
    """Estado por plataforma con el número de cookies que lo justifican."""
    details: dict[str, dict] = {}
    for platform_id in PLATFORM_ORDER:
        rule = PLATFORM_RULES[platform_id]
        matching = 0
        for cookie in cookies:
            if _cookie_is_ignored(cookie):
                continue
            domain = (cookie.get("domain") or "").lower()
            name = (cookie.get("name") or "").lower()
            if _cookie_is_excluded(cookie, rule):
                continue
            in_identity = any(
                _host_matches(domain, candidate)
                for candidate in rule.get("identity_domains", ())
            )
            in_domain = any(
                _domain_matches(domain, candidate)
                for candidate in rule["domains"]
            )
            if not (in_identity or in_domain):
                continue
            names = (
                {n.lower() for n in rule.get("identity_cookie_names", ())}
                if in_identity
                else {n.lower() for n in rule.get("cookie_names", ())}
            )
            if name in names:
                matching += 1
        covered = platform_covered(
            cookies,
            platform_id,
            kaltura_ks=kaltura_ks if platform_id == "kaltura" else False,
        )
        entry = {
            "label": rule["label"],
            "covered": covered,
            "cookies": matching,
        }
        if platform_id == "kaltura":
            entry["ks"] = bool(kaltura_ks)
        details[platform_id] = entry
    return details


def read_saved_cookies() -> tuple[list[dict], list[dict], float]:
    """Cookies combinadas de todos los archivos de sesión.

    Devuelve (cookies, files, latest_mtime). El archivo principal tiene
    prioridad sobre los de respaldo, igual que en el crawler.
    """
    files: list[dict] = []
    cookies_by_key: dict[tuple, dict] = {}
    latest_mtime = 0.0
    # De menor a mayor prioridad: el principal se aplica al final.
    for path in reversed(SESSION_FILES):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            files.append({"name": path.name, "cookies": None, "error": "No se pudo leer"})
            continue
        cookies = raw.get("cookies", []) or []
        files.append({"name": path.name, "cookies": len(cookies), "error": None})
        latest_mtime = max(latest_mtime, path.stat().st_mtime)
        for cookie in cookies:
            key = (
                cookie.get("name"),
                (cookie.get("domain") or "").lower(),
                cookie.get("path") or "/",
            )
            cookies_by_key[key] = cookie
    # El orden de `files` se invierte al recorrer; se devuelve en el orden
    # natural (principal primero) para que el dashboard sea predecible.
    files.reverse()
    return list(cookies_by_key.values()), files, latest_mtime


def session_info() -> dict:
    cookies, files, latest_mtime = read_saved_cookies()
    has_ks = ks_available()
    if has_ks:
        # El token KS de KMC se guarda aparte porque Playwright no exporta
        # sessionStorage; también cuenta como sesión guardada.
        latest_mtime = max(latest_mtime, ks_mtime())
    if not files and not has_ks:
        return {
            "exists": False,
            "cookies": 0,
            "domains": [],
            "saved_at": None,
            "files": [],
            "platforms": {key: False for key in PLATFORM_ORDER},
            "platform_details": platform_details([]),
            "complete": False,
            "kaltura_ks": False,
        }

    domains = sorted({(c.get("domain") or "").lower() for c in cookies if c.get("domain")})
    details = platform_details(cookies, kaltura_ks=has_ks)
    platforms = {key: bool(details[key]["covered"]) for key in PLATFORM_ORDER}
    return {
        "exists": True,
        "cookies": len(cookies),
        "domains": domains,
        "saved_at": (
            datetime.fromtimestamp(latest_mtime).isoformat(timespec="seconds")
            if latest_mtime
            else None
        ),
        "files": files,
        "platforms": platforms,
        "platform_details": details,
        "complete": all(platforms.values()),
        "kaltura_ks": has_ks,
    }

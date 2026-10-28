"""
Enriquecimiento con Veeva Vault: usa el Approval ID como LLAVE para obtener
el estado y fechas reales del documento.

Los nombres de campos varían por Vault (p. ej. 'document_number__v', 'status__v',
'expiration_date__c'). Ajusta VQL_TEMPLATE a tu configuración.
"""
import json
import logging
from typing import Any

import requests

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("veeva")

# AJUSTAR: campos y objeto según tu Vault
VQL_TEMPLATE = (
    "SELECT id, name__v, document_number__v, status__v, expiration_date__c, "
    "effective_date__v, lifecycle_state__v "
    "FROM documents WHERE document_number__v = '{doc_number}'"
)

_session_id: str | None = None


def _base() -> str:
    if not settings.veeva_dns:
        raise ValueError("VEEVA_DNS no configurado")
    return f"https://{settings.veeva_dns}/api/{settings.veeva_api_version}"


def _login() -> str | None:
    global _session_id
    if _session_id:
        return _session_id
    if not settings.veeva_username or not settings.veeva_password:
        log.warning("Veeva: faltan credenciales (VEEVA_USERNAME/VEEVA_PASSWORD)")
        return None
    try:
        r = requests.post(
            f"{_base()}/auth",
            data={"username": settings.veeva_username, "password": settings.veeva_password},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("responseStatus") == "SUCCESS":
            _session_id = data.get("sessionId")
            if _session_id:
                log.debug("Veeva: sesión iniciada correctamente")
        else:
            log.warning("Veeva: login fallido: %s", data.get("errors") or data)
    except requests.RequestException as exc:
        log.warning("Veeva: error en login: %s", exc)
    except (ValueError, KeyError) as exc:
        log.warning("Veeva: respuesta inesperada en login: %s", exc)
    return _session_id


def lookup_approval_id(approval_id: str) -> dict | None:
    """Devuelve el detalle del documento o None."""
    if not settings.enable_veeva or not approval_id:
        return None
    try:
        sid = _login()
        if not sid:
            return None
        safe = str(approval_id).replace("'", "''")
        r = requests.get(
            f"{_base()}/query",
            params={"q": VQL_TEMPLATE.format(doc_number=safe)},
            headers={"Authorization": sid, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("responseStatus") == "SUCCESS" and data.get("data"):
            doc = data["data"][0]
            log.debug("Veeva: encontrado documento para %s", approval_id)
            return doc
        if data.get("errors"):
            log.debug("Veeva: sin resultados o error para %s: %s", approval_id, data.get("errors"))
    except requests.RequestException as exc:
        log.warning("Veeva lookup falló para %s (red/HTTP): %s", approval_id, exc)
    except (ValueError, KeyError) as exc:
        log.warning("Veeva lookup falló para %s (parseo): %s", approval_id, exc)
    except Exception as exc:
        log.warning("Veeva lookup falló para %s: %s", approval_id, exc)
    return None


def to_json(detail: dict | None) -> str | None:
    if not detail:
        return None
    try:
        return json.dumps(detail, ensure_ascii=False)
    except (TypeError, ValueError):
        return None

"""
Integración con Kaltura para videos. Si ENABLE_KALTURA=false, solo se detectan
los entry_id y se registra el video sin metadatos de vencimiento.
"""
import logging
import re
from datetime import datetime

from app.config import get_settings
from app.scanner.extractors import analyze_text, classify_status

settings = get_settings()
log = logging.getLogger("kaltura")

# Patrones típicos del reproductor Kaltura
ENTRY_ID_PATTERNS = [
    re.compile(r"entry_id[=/:\"']+\s*([0-9]_[a-z0-9]{8})", re.I),
    re.compile(r"/entry_id/([0-9]_[a-z0-9]{8})", re.I),
    re.compile(r"/entryId/([0-9]_[a-z0-9]{8})", re.I),
    re.compile(r"\"entryId\"\s*:\s*\"([0-9]_[a-z0-9]{8})\"", re.I),
    re.compile(r"\b([0-9]_[a-z0-9]{8})\b"),
]


def find_kaltura_entry_ids(html: str) -> list[str]:
    found: list[str] = []
    if not html:
        return found
    if "kaltura" not in html.lower():
        return found
    for p in ENTRY_ID_PATTERNS[:-1]:
        for m in p.findall(html):
            if m not in found:
                found.append(m)
    return found


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from KalturaClient import KalturaClient, KalturaConfiguration  # import diferido
    except ImportError as exc:
        raise ImportError("KalturaApiClient no está instalado. Ejecuta: pip install KalturaApiClient") from exc

    if not settings.kaltura_partner_id or not settings.kaltura_admin_secret:
        raise ValueError("Faltan KALTURA_PARTNER_ID o KALTURA_ADMIN_SECRET")
    cfg = KalturaConfiguration(settings.kaltura_partner_id)
    cfg.serviceUrl = settings.kaltura_service_url
    client = KalturaClient(cfg)
    ks = client.session.start(
        settings.kaltura_admin_secret,
        "scanner",
        2,
        settings.kaltura_partner_id,
    )
    client.setKs(ks)
    _client = client
    return client


def analyze_kaltura_entry(entry_id: str, market: str | None = None) -> dict:
    """Devuelve el mismo formato que analyze_text, más metadatos del video."""
    base = {
        "veeva_id": None,
        "expiry_year": None,
        "expiry_date": None,
        "confidence": 0.0,
        "status": "SIN_DATO",
        "method": "kaltura",
        "snippet": f"Kaltura entry {entry_id} (sin acceso a API)",
    }
    if not settings.enable_kaltura:
        return base
    try:
        entry = _get_client().media.get(entry_id)
        blob = " ".join(
            filter(
                None,
                [
                    getattr(entry, "name", None),
                    getattr(entry, "description", None),
                    getattr(entry, "tags", None),
                ],
            )
        )
        res = analyze_text(blob, market)

        # Fecha de fin programada en Kaltura (si existe)
        end_ts = getattr(entry, "endDate", None)
        if end_ts and end_ts > 0:
            try:
                end = datetime.utcfromtimestamp(end_ts).date()
                res["expiry_date"], res["expiry_year"] = end, end.year
                res["confidence"] = max(res.get("confidence", 0.0), 0.95)
                res["status"] = classify_status(end.year, end)
            except (OSError, OverflowError, ValueError):
                pass

        res["method"] = "kaltura"
        res["snippet"] = f"{getattr(entry, 'name', '')} | {getattr(entry, 'tags', '') or ''}"[:300]
        return res
    except Exception as exc:
        log.warning("Kaltura entry %s falló: %s", entry_id, exc)
        base["snippet"] = f"Kaltura entry {entry_id} (error API: {exc})"[:300]
        return base

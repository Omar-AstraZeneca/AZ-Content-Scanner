"""
Funciones de IA. Diseñadas para modelos LOCALES (Ollama) para no enviar
material fuera de la organización. Si ENABLE_* está en false, hay un
comportamiento determinista (reglas) que funciona sin IA.
"""
import base64
import json
import logging

import requests

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("ai_helper")

VLM_PROMPT = (
    "Analiza esta imagen de material promocional farmacéutico. "
    "Busca un código de aprobación (Veeva Approval ID / Promomats) y un año o fecha de vencimiento "
    "(suelen estar en letra pequeña, normalmente en el borde inferior). "
    'Responde SOLO con JSON: {"veeva_id": string|null, "expiry_year": number|null, "confidence": number entre 0 y 1}'
)


def vlm_extract(image_bytes: bytes) -> dict | None:
    if not settings.enable_vlm:
        return None
    if not image_bytes:
        return None
    try:
        payload = {
            "model": settings.vlm_model,
            "prompt": VLM_PROMPT,
            "images": [base64.b64encode(image_bytes).decode()],
            "stream": False,
            "format": "json",
        }
        r = requests.post(settings.vlm_endpoint, json=payload, timeout=180)
        r.raise_for_status()
        resp = r.json()
        content = resp.get("response") or resp.get("message", {}).get("content") or "{}"
        if isinstance(content, str):
            data = json.loads(content)
        elif isinstance(content, dict):
            data = content
        else:
            return None
        if not isinstance(data, dict):
            return None
        # Normalizar valores
        veeva_id = data.get("veeva_id")
        if veeva_id == "":
            veeva_id = None
        expiry_year = data.get("expiry_year")
        if isinstance(expiry_year, str) and expiry_year.isdigit():
            try:
                expiry_year = int(expiry_year)
            except ValueError:
                expiry_year = None
        conf = data.get("confidence")
        try:
            conf = float(conf) if conf is not None else 0.6
        except (TypeError, ValueError):
            conf = 0.6
        return {
            "veeva_id": veeva_id,
            "expiry_year": expiry_year,
            "confidence": max(0.0, min(1.0, conf)),
        }
    except requests.RequestException as exc:
        log.warning("VLM no disponible (red): %s", exc)
        return None
    except (ValueError, KeyError) as exc:
        log.warning("VLM no disponible (parseo): %s", exc)
        return None
    except Exception as exc:
        log.warning("VLM no disponible: %s", exc)
        return None



def recommend_action(status: str, asset_type: str, year: int | None) -> str:
    """Recomendación determinista (siempre disponible, sin IA)."""
    if status == "VENCIDO":
        return f"ELIMINAR o ACTUALIZAR de inmediato: el {asset_type} venció en {year}. Solicitar re-aprobación en Veeva."
    if status == "PROXIMO_A_VENCER":
        return f"PLANIFICAR renovación: el {asset_type} vence en {year}. Iniciar proceso de re-aprobación."
    if status == "REVISAR":
        return (f"REVISAR manualmente: se detectó {year} con baja confianza en este {asset_type}; "
                "podría ser una fecha de elaboración o de publicación y no el vencimiento.")
    if status == "SIN_DATO":
        return f"REVISAR manualmente: no se detectó código ni fecha en este {asset_type}."
    return "Sin acción requerida."


def executive_summary(market: str, stats: dict) -> str:
    """Resumen ejecutivo. Usa LLM local si está habilitado; si no, plantilla."""
    base = (
        f"Mercado {market}: {stats.get('pages', 0)} páginas escaneadas, "
        f"{stats.get('total', 0)} activos analizados. "
        f"{stats.get('VENCIDO', 0)} vencidos, {stats.get('PROXIMO_A_VENCER', 0)} próximos a vencer, "
        f"{stats.get('REVISAR', 0)} por revisar (fecha poco fiable), "
        f"{stats.get('SIN_DATO', 0)} sin dato y {stats.get('VIGENTE', 0)} vigentes."
    )
    if not settings.enable_llm_summary:
        return base
    try:
        prompt = (
            "Eres un analista de compliance. Redacta en español un resumen ejecutivo de 3 frases, "
            "con tono profesional y accionable, basado en estos datos:\n" + json.dumps(stats, ensure_ascii=False)
        )
        endpoint = settings.vlm_endpoint
        r = requests.post(
            endpoint,
            json={"model": settings.llm_model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        r.raise_for_status()
        return r.json().get("response", base).strip() or base
    except Exception as exc:
        log.warning("LLM no disponible, se usa plantilla: %s", exc)
        return base
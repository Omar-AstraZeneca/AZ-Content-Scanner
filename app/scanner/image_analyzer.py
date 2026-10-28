"""
Análisis de imágenes con códigos/fechas "quemados".
Estrategia híbrida:
  1) OCR en franja inferior (rápido, local)
  2) OCR en imagen completa (si 1 no encontró nada)
  3) Modelo de visión (VLM) opcional si 1 y 2 fallan

La vía principal para obtener los bytes de la imagen es el propio navegador
(Playwright), que ya tiene sesión, cookies y cabeceras de navegador. La
descarga con `requests` queda solo como último recurso: muchos sitios (por
ejemplo AEM con CDN) devuelven 403 a clientes que no son un navegador.

Los SVG no se analizan: son dibujos vectoriales (iconos, logotipos) sin texto
«quemado» que un OCR pueda leer.
"""
import io
import logging
import os
import re
import shutil
from html import unescape

from PIL import Image, ImageOps, ImageFilter

from app.config import get_settings
from app.scanner.extractors import REVIEW_CONFIDENCE, analyze_text, classify_status
from app.scanner.ai_helper import vlm_extract
from app.scanner.netguard import safe_get

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None

settings = get_settings()
log = logging.getLogger("image_analyzer")


def _find_tesseract():
    cmd = getattr(settings, "tesseract_cmd", "") or ""
    if cmd and os.path.exists(cmd):
        return cmd
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%ProgramFiles%\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    found = shutil.which("tesseract")
    if found:
        return found
    return ""


_tesseract_path = _find_tesseract()
if pytesseract and _tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = _tesseract_path
    try:
        settings.tesseract_cmd = _tesseract_path
    except Exception:
        pass



# Tamaño máximo aceptado para una imagen descargada (evita agotar memoria)
MAX_IMAGE_BYTES = 8_000_000

# Evita repetir el mismo aviso cientos de veces
_ocr_warning_shown = False


def tesseract_available() -> bool:
    """True si el ejecutable de Tesseract existe y se puede usar."""
    if pytesseract is None:
        return False
    command = settings.tesseract_cmd or "tesseract"
    return bool(shutil.which(command))


def download_image(url: str, referer: str | None = None, url_ok=None) -> bytes | None:
    """Descarga de respaldo con requests.

    Cada salto de redirección se valida (lista autorizada `url_ok` + host con IP pública) y
    se limita el tamaño mientras se descarga. Si el sitio responde 403, la imagen se omite
    (la vía principal es el navegador); el motivo queda en DEBUG porque el pipeline emite
    un único aviso resumen por escaneo.
    """
    try:
        headers = {
            "User-Agent": settings.user_agent,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        data = safe_get(url, headers=headers, timeout=30, max_bytes=MAX_IMAGE_BYTES, url_ok=url_ok)
        if data is None:
            log.debug("Imagen rechazada, demasiado grande o no permitida: %s", url)
        return data
    except Exception as exc:
        log.debug("No se pudo descargar imagen %s: %s", url, exc)
        return None


def _preprocess(img: Image.Image, scale: int = 3) -> Image.Image:
    gray = img.convert("L")
    gray = ImageOps.autocontrast(gray)
    gray = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray


def _ocr(img: Image.Image) -> str:
    global _ocr_warning_shown
    if pytesseract is None or not tesseract_available():
        if not _ocr_warning_shown:
            _ocr_warning_shown = True
            log.error(
                "TESSERACT NO ESTÁ INSTALADO: el OCR está desactivado y las imágenes "
                "no se leerán. Instálalo con tesseract (Windows: https://github.com/UB-Mannheim/tesseract/wiki, "
                "macOS: brew install tesseract tesseract-lang, Linux: paquete tesseract-ocr). "
                "Configura TESSERACT_CMD en .env si no está en PATH."
            )
        return ""
    try:
        text = pytesseract.image_to_string(img, lang=settings.ocr_langs, config="--psm 6")
        # Segunda pasada con fondo invertido (texto claro sobre fondo oscuro)
        try:
            inv = ImageOps.invert(img)
            text += "\n" + pytesseract.image_to_string(inv, lang=settings.ocr_langs, config="--psm 6")
        except Exception:
            pass
        return text.strip()
    except Exception as exc:
        log.warning("Fallo OCR: %s", exc)
        return ""



def _reliable(result: dict) -> bool:
    """True si hay un ID, o una fecha con confianza suficiente (no un año suelto dudoso)."""
    return bool(
        result["veeva_id"]
        or (result["expiry_year"] and result.get("expiry_confidence", 0.0) >= REVIEW_CONFIDENCE)
    )


def _looks_like_svg(data: bytes) -> bool:
    """Detecta un SVG por su contenido (los bytes empiezan por «<svg» o «<?xml»)."""
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower())


def _svg_text(data: bytes) -> str:
    """Extrae el texto de un SVG (<text>, <tspan>, <title>, <desc>).

    Un SVG no se puede abrir con Pillow/OCR, pero sí se puede leer su texto: los
    IDs y fechas suelen estar en nodos de texto o en metadatos del propio archivo.
    """
    try:
        raw = data.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    parts: list[str] = []
    for tag in ("text", "tspan", "title", "desc"):
        for match in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", raw, flags=re.IGNORECASE | re.DOTALL):
            clean = re.sub(r"<[^>]+>", " ", match)
            clean = unescape(clean)
            if clean.strip():
                parts.append(clean.strip())
    # Texto suelto en atributos aria-label / alt
    for attr in ("aria-label", "alt"):
        for match in re.findall(rf'{attr}="([^"]+)"', raw, flags=re.IGNORECASE):
            if match.strip():
                parts.append(unescape(match.strip()))
    return " ".join(dict.fromkeys(parts))



def analyze_image_bytes(data: bytes, market: str | None = None) -> dict | None:
    """Devuelve dict con resultado o None si la imagen es muy pequeña/inválida."""
    if not data:
        return None
    if _looks_like_svg(data):
        text = _svg_text(data)
        if not text:
            return None
        result = analyze_text(text, market)
        result["method"] = "svg"
        result["ocr_text"] = text[:500]
        result["width"] = result["height"] = 0
        return result

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        # Las imágenes con paleta y transparencia dan avisos de Pillow; se normalizan
        if img.mode in ("P", "LA", "P;L"):
            try:
                img = img.convert("RGBA")
            except Exception:
                img = img.convert("RGB")
        elif img.mode == "1":
            img = img.convert("L")
    except Exception:
        return None

    if img.width < settings.min_image_width or img.height < settings.min_image_height:
        # Analizar incluso imágenes pequeñas si hay texto
        if img.width < 50 or img.height < 20:
            return None


    # 1) Imagen completa primero para mayor cobertura
    text_full = _ocr(_preprocess(img, scale=2))
    result = analyze_text(text_full, market)
    method = "ocr"
    text = text_full

    # 2) Franja inferior si no hay resultado confiable
    if not _reliable(result):
        h = img.height
        strip = img.crop((0, int(h * (1 - settings.ocr_bottom_strip)), img.width, h))
        text_strip = _ocr(_preprocess(strip))
        r_strip = analyze_text(text_strip, market)
        if _reliable(r_strip) or (
            not (result.get("veeva_id") or result.get("expiry_year"))
            and (r_strip.get("veeva_id") or r_strip.get("expiry_year"))
        ):
            result, text = r_strip, text_strip
        elif _reliable(r_strip):
            result, text = r_strip, text_strip



    # 3) Modelo de visión (opcional)
    if settings.enable_vlm and not _reliable(result):
        vlm = vlm_extract(data)
        if vlm and (vlm.get("veeva_id") or vlm.get("expiry_year")):
            try:
                year = int(vlm["expiry_year"]) if vlm.get("expiry_year") else None
            except (TypeError, ValueError):
                year = None
            try:
                vlm_conf = float(vlm.get("confidence") or 0.6)
            except (TypeError, ValueError):
                vlm_conf = 0.6
            result = {
                "veeva_id": vlm.get("veeva_id"),
                "expiry_year": year,
                "expiry_date": None,
                "confidence": vlm_conf,
                "expiry_confidence": vlm_conf,
                "status": classify_status(year, None, confidence=vlm_conf if year else None),
            }
            method = "vlm"
            text = f"[VLM] {vlm}"

    result["method"] = method
    result["ocr_text"] = text[:500]
    result["width"], result["height"] = img.width, img.height
    return result
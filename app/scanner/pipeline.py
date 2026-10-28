"""
Pipeline principal: para cada página -> texto, imágenes, videos, PDFs -> Asset en BD.
Admite cancelación cooperativa (se comprueba entre páginas) y deja constancia de
cada página visitada para poder auditar la cobertura del escaneo.

El ScanRun lo crea normalmente el endpoint (scan.py) para poder devolver su id al
navegador; aquí se reutiliza mediante `existing_run_id`. Si no se indica (por
ejemplo, desde run_scan.py), el pipeline crea el suyo.

Un escaneo solo se considera «Completado» si se pudo leer al menos una página. Si
ninguna cargó (sin red, VPN apagada, sitio bloqueado), termina como «Error» con el
motivo real, en lugar de un «Completado» con 0 IDs que oculta el problema.

Imágenes: los SVG se ignoran (son iconos vectoriales sin texto «quemado»). Para las
demás, los bytes los aporta primero el navegador; la descarga directa con `requests`
es solo un último recurso y, si el sitio la rechaza con 403, se avisa UNA sola vez por
escaneo en lugar de repetir el aviso por cada imagen.
"""
import asyncio
import io
import json
import logging
import sys
from contextlib import aclosing
from datetime import datetime
from urllib.parse import unquote, urljoin, urlsplit

# Fix for Playwright on Windows
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import fitz  # PyMuPDF
from bs4 import BeautifulSoup
from sqlalchemy import func, update

from app.config import get_settings
from app.database import SessionLocal
from app.models import Asset, AssetOccurrence, ScanRun, ScannedPage
from app.scanner import cancellation
from app.scanner.ai_helper import executive_summary, recommend_action
from app.scanner.crawler import AUTH_STOPPED, crawl_market, is_allowed_url, is_vector_image, PageResult
from app.scanner.extractors import REVIEW_CONFIDENCE, analyze_text, classify_status
from app.scanner.image_analyzer import analyze_image_bytes, download_image, tesseract_available
from app.scanner.kaltura_client import find_kaltura_entry_ids, analyze_kaltura_entry
from app.scanner.netguard import is_public_http_url, safe_get
from app.scanner.veeva_client import lookup_approval_id, to_json

settings = get_settings()
log = logging.getLogger("pipeline")


# Límite de tamaño de un PDF descargado (evita agotar memoria)
MAX_PDF_BYTES = 25_000_000

# Páginas de un PDF que se leen desde el principio (la última siempre se lee también)
PDF_LEADING_PAGES = 5

# Si un PDF no tiene texto (escaneado), se pasan por OCR sus primeras páginas
PDF_OCR_PAGES = 2
PDF_MIN_TEXT_CHARS = 40

# Atributos de un video/iframe donde suele venir el título o el código del material
VIDEO_TEXT_ATTRS = ("title", "aria-label", "data-title", "data-name", "data-video-title", "alt")


def load_markets() -> dict:
    with open("data/markets.json", encoding="utf-8") as f:
        return {m["code"]: m for m in json.load(f)["markets"]}


class AssetRegistry:
    """Activos ya analizados en este escaneo (imagen, PDF o video, por URL).

    Sirve para no repetir el OCR, pero SIN perder dónde aparece cada activo: cuando el mismo
    banner vencido sale en otra página, se registra esa aparición contra el activo original.

    También guarda una caché de consultas a Veeva: un mismo Approval ID se consulta una sola
    vez por escaneo (antes se repetía por cada imagen o página donde aparecía).
    """

    def __init__(self) -> None:
        self._ids: dict[str, int | None] = {}
        self._pairs: set[tuple[int, str]] = set()
        self._veeva: dict[str, dict | None] = {}
        # Imágenes que el servidor rechazó (403): se avisa una sola vez por escaneo
        self.blocked_images = 0

    def __contains__(self, key: str) -> bool:
        return key in self._ids

    def mark(self, key: str) -> None:
        """Reserva la clave antes de analizar (si no genera activo, no habrá apariciones)."""
        self._ids.setdefault(key, None)

    def attach(self, key: str, asset: Asset) -> None:
        self._ids[key] = asset.id
        self._pairs.add((asset.id, asset.page_url))

    def add_occurrence(self, db, key: str, page_url: str) -> None:
        asset_id = self._ids.get(key)
        if asset_id is None or (asset_id, page_url) in self._pairs:
            return
        self._pairs.add((asset_id, page_url))
        db.add(AssetOccurrence(asset_id=asset_id, page_url=page_url))
        db.execute(update(Asset).where(Asset.id == asset_id).values(occurrences=Asset.occurrences + 1))

    def veeva_lookup(self, approval_id: str) -> dict | None:
        """Consulta a Veeva con caché por escaneo."""
        if approval_id not in self._veeva:
            self._veeva[approval_id] = lookup_approval_id(approval_id)
        return self._veeva[approval_id]


def _has_data(res: dict) -> bool:
    """True si el resultado trae un ID o una fecha utilizable."""
    return bool(res.get("veeva_id") or res.get("expiry_year"))


def _save_asset(db, run: ScanRun, market: str, page: PageResult, asset_type: str,
                asset_url: str | None, snippet: str, res: dict, method: str,
                registry: AssetRegistry | None = None) -> Asset:
    detail = None
    if res.get("veeva_id"):
        detail_dict = registry.veeva_lookup(res["veeva_id"]) if registry else lookup_approval_id(res["veeva_id"])
        detail = to_json(detail_dict)
        # Si Veeva trae fecha de expiración real, tiene prioridad
        if detail_dict:
            exp = detail_dict.get("expiration_date__c")
            if exp:
                try:
                    d = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                    res["expiry_date"], res["expiry_year"] = d, d.year
                    res["status"] = classify_status(d.year, d)
                    res["confidence"] = 0.98
                    method = "veeva_api"
                except ValueError:
                    pass

    asset = Asset(
        run_id=run.id, market=market, page_url=page.url, asset_type=asset_type,
        asset_url=asset_url, snippet=(snippet or "")[:500],
        veeva_id=res.get("veeva_id"), expiry_year=res.get("expiry_year"),
        expiry_date=res.get("expiry_date"), status=res["status"],
        detection_method=method, confidence=res.get("confidence", 0.0),
        veeva_detail=detail, evidence_path=page.screenshot_path,
        recommendation=recommend_action(res["status"], asset_type, res.get("expiry_year")),
    )
    db.add(asset)
    db.flush()  # asigna asset.id (lo necesita el registro de apariciones)
    return asset


def _analyze_page_text(db, run, market, page: PageResult, registry: AssetRegistry) -> int:
    # Prioridad al footer (donde suele estar el año de vencimiento)
    source = page.footer_text or page.visible_text[-1200:]
    res = analyze_text(source, market)
    if res["status"] in ("SIN_DATO", "REVISAR") and page.visible_text:
        # Intento sobre todo el texto; se conserva el resultado con fecha más fiable
        full = analyze_text(page.visible_text, market)
        if full["expiry_confidence"] > res["expiry_confidence"] or (
            not res["veeva_id"] and full["veeva_id"] and full["expiry_confidence"] >= res["expiry_confidence"]
        ):
            res = full
    snippet = (page.footer_text or page.visible_text[-300:]).strip().replace("\n", " ")
    _save_asset(db, run, market, page, "texto", page.url, snippet, res, "regex", registry)
    return 1


def _analyze_images(db, run, market, page: PageResult, seen: AssetRegistry, allowlist: list[str]) -> tuple[int, int, int]:
    """Analiza TODAS las imágenes de la página.

    Devuelve (activos guardados, imágenes analizadas, imágenes omitidas).

    Orden de obtención de los bytes:
      1) Los que obtuvo el navegador (no genera peticiones nuevas ni 403).
      2) Descarga directa con requests, solo como último recurso.

    Se incluyen imágenes de <img src>, srcset, <picture><source>, lazy-load,
    og:image, fondos CSS y pósters de vídeo. También se analizan SVG.
    """
    soup = BeautifulSoup(page.html, "lxml")
    browser_images: dict[str, bytes] = getattr(page, "image_bytes", None) or {}
    saved = analyzed = skipped = 0

    # Metadatos HTML por URL (alt/title/nombre de archivo), para combinarlos con el OCR
    meta_by_url: dict[str, str] = {}
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            src = img.get(attr)
            if not src or str(src).startswith("data:"):
                continue
            full = urljoin(page.url, src)
            if not is_allowed_url(full, allowlist):
                continue
            blob = " ".join(filter(None, [img.get("alt"), img.get("title"), unquote(full.rsplit("/", 1)[-1])]))
            if blob:
                meta_by_url[full] = blob

    candidates: list[str] = []
    seen_urls: set[str] = set()
    for candidate in [*(getattr(page, "image_urls", None) or []), *meta_by_url.keys()]:
        if candidate in seen_urls:
            continue
        seen_urls.add(candidate)
        candidates.append(candidate)

    for full in candidates:
        if full in seen:
            seen.add_occurrence(db, full, page.url)
            continue
        seen.mark(full)

        try:
            meta_blob = meta_by_url.get(full) or unquote(full.rsplit("/", 1)[-1])
            meta_res = analyze_text(meta_blob, market)
            meta_has_data = _has_data(meta_res)

            data = browser_images.get(full)
            if data is None:
                data = download_image(full, referer=page.url, url_ok=lambda u: is_allowed_url(u, allowlist))
                if data is None:
                    seen.blocked_images += 1
            vis = analyze_image_bytes(data, market) if data else None

            if vis is None:
                # Sin imagen analizable (inaccesible, inválida o icono pequeño)
                skipped += 1
                if meta_has_data:
                    seen.attach(full, _save_asset(
                        db, run, market, page, "imagen", full, meta_blob, meta_res, "regex", seen
                    ))
                    saved += 1
                continue

            analyzed += 1
            res, method = vis, vis["method"]
            vis_reliable = bool(
                vis["veeva_id"]
                or (vis["expiry_year"] and vis.get("expiry_confidence", 0.0) >= REVIEW_CONFIDENCE)
            )
            if not vis_reliable and meta_has_data and (
                meta_res["veeva_id"] or meta_res["expiry_confidence"] >= vis.get("expiry_confidence", 0.0)
            ):
                res, method = meta_res, "regex"

            if not _has_data(res) and not meta_has_data:
                continue

            seen.attach(full, _save_asset(
                db, run, market, page, "imagen", full, vis.get("ocr_text") or meta_blob, res, method, seen
            ))
            saved += 1
        except Exception as exc:
            # Una imagen defectuosa no debe detener el análisis de la página
            skipped += 1
            log.warning("Imagen %s omitida por error: %s", full, exc)
    return saved, analyzed, skipped


def _video_blob(tag, full_url: str) -> str:
    """Texto candidato de un video: atributos descriptivos + data-* + URL."""
    parts = [tag.get(attr) for attr in VIDEO_TEXT_ATTRS]
    parts += [value for key, value in tag.attrs.items() if key.startswith("data-") and isinstance(value, str)]
    parts.append(unquote(full_url))
    return " ".join(str(p) for p in parts if p)


def _analyze_videos(db, run, market, page: PageResult, seen: AssetRegistry) -> int:
    count = 0
    # Kaltura
    for entry_id in find_kaltura_entry_ids(page.html):
        key = f"kaltura:{entry_id}"
        if key in seen:
            seen.add_occurrence(db, key, page.url)
            continue
        seen.mark(key)
        res = analyze_kaltura_entry(entry_id, market)
        seen.attach(key, _save_asset(
            db, run, market, page, "video", f"kaltura://{entry_id}", res.pop("snippet", ""), res, "kaltura", seen
        ))
        count += 1

    # Otros videos (video/iframe)
    soup = BeautifulSoup(page.html, "lxml")
    for tag in soup.find_all(["video", "iframe"]):
        src = tag.get("src") or tag.get("data-src") or ""
        if not src or "kaltura" in src.lower():
            continue
        if not any(k in src.lower() for k in ("youtube", "vimeo", "brightcove", ".mp4", "wistia")):
            continue
        full = urljoin(page.url, src)
        if urlsplit(full).scheme.lower() not in ("http", "https"):
            continue  # descarta javascript:, data:, etc.
        if full in seen:
            seen.add_occurrence(db, full, page.url)
            continue
        seen.mark(full)
        res = analyze_text(_video_blob(tag, full), market)
        seen.attach(full, _save_asset(
            db, run, market, page, "video", full, tag.get("title") or full, res, "regex", seen
        ))
        count += 1
    return count


def _pdf_text(data: bytes) -> str:
    """Texto de un PDF: primeras páginas + última. Si no trae texto (escaneado), usa OCR."""
    doc = fitz.open(stream=io.BytesIO(data), filetype="pdf")
    try:
        total = len(doc)
        indices = list(range(min(total, PDF_LEADING_PAGES)))
        if total and (total - 1) not in indices:
            indices.append(total - 1)
        text = "\n".join(doc[i].get_text() for i in indices)

        # Metadatos del documento (título/asunto suelen incluir el código del material)
        meta = doc.metadata or {}
        meta_text = " ".join(str(meta.get(k) or "") for k in ("title", "subject", "keywords"))

        if len(text.strip()) < PDF_MIN_TEXT_CHARS and tesseract_available():
            # PDF escaneado: se renderizan las primeras páginas y se pasan por el OCR
            ocr_parts = []
            for i in range(min(total, PDF_OCR_PAGES)):
                pix = doc[i].get_pixmap(dpi=150)
                result = analyze_image_bytes(pix.tobytes("png"))
                if result and result.get("ocr_text"):
                    ocr_parts.append(result["ocr_text"])
            text = f"{text}\n" + "\n".join(ocr_parts)
        return f"{meta_text}\n{text}"
    finally:
        doc.close()


def _analyze_pdfs(db, run, market, page: PageResult, seen: AssetRegistry, allowlist: list[str]) -> int:
    count = 0
    browser_pdfs: dict[str, bytes] = getattr(page, "pdf_bytes", None) or {}

    for pdf_url in page.pdf_links:
        if not is_allowed_url(pdf_url, allowlist):
            continue
        if pdf_url in seen:
            seen.add_occurrence(db, pdf_url, page.url)
            continue
        seen.mark(pdf_url)
        try:
            data = browser_pdfs.get(pdf_url)
            if data is None:
                # Respaldo: cada salto de redirección se valida; máximo MAX_PDF_BYTES
                data = safe_get(
                    pdf_url, headers={"User-Agent": settings.user_agent}, timeout=45,
                    max_bytes=MAX_PDF_BYTES, url_ok=lambda u: is_allowed_url(u, allowlist),
                )
                if data is None:
                    raise ValueError("descarga rechazada o demasiado grande")
            if len(data) > MAX_PDF_BYTES:
                log.warning("PDF demasiado grande, se omite: %s", pdf_url)
                continue

            try:
                text = _pdf_text(data)
            except Exception as exc:
                log.warning("PDF %s: fallo al extraer texto, se prueba solo con nombre de archivo: %s", pdf_url, exc)
                text = ""
            # El nombre del archivo también suele traer el código del material
            filename = unquote(pdf_url.rsplit("/", 1)[-1])
            res = analyze_text(f"{filename}\n{text}", market)
            if not _has_data(res) and text:
                # Intento con todo el texto si el combinado no dio resultado
                res2 = analyze_text(text, market)
                if _has_data(res2):
                    res = res2
            seen.attach(pdf_url, _save_asset(
                db, run, market, page, "pdf", pdf_url, text[:300].replace("\n", " ") or filename, res, "regex", seen
            ))
            count += 1
        except Exception as exc:
            log.warning("PDF %s falló: %s", pdf_url, exc)
            # Aunque no se pueda abrir el PDF, el nombre del archivo suele traer el código del material
            try:
                filename = unquote(pdf_url.rsplit("/", 1)[-1])
            except Exception:
                filename = pdf_url
            name_res = analyze_text(filename, market)
            if _has_data(name_res):
                seen.attach(pdf_url, _save_asset(
                    db, run, market, page, "pdf", pdf_url, filename, name_res, "regex", seen
                ))
                count += 1
    return count



def _process_page(db, run: ScanRun, market: str, page: PageResult,
                  registry: AssetRegistry, allowlist: list[str]) -> None:
    """Analiza UNA página (texto, imágenes, videos, PDFs) y guarda el resultado.

    Es código SÍNCRONO y pesado (OCR, PyMuPDF, descargas, Veeva, base de datos): se ejecuta
    en un hilo con `asyncio.to_thread` para que el servidor siga atendiendo al dashboard y al
    botón Cancelar mientras tanto. Se usa la misma sesión de BD de forma secuencial.
    """
    run_id = run.id
    assets_before = db.query(func.count(Asset.id)).filter(Asset.run_id == run_id).scalar() or 0

    _analyze_page_text(db, run, market, page, registry)
    _, analyzed, skipped = _analyze_images(db, run, market, page, registry, allowlist)
    _analyze_videos(db, run, market, page, registry)
    _analyze_pdfs(db, run, market, page, registry, allowlist)

    run.images_analyzed += analyzed
    run.images_skipped += skipped
    db.flush()
    assets_after = db.query(func.count(Asset.id)).filter(Asset.run_id == run_id).scalar() or 0
    run.assets_found = assets_after

    db.add(ScannedPage(
        run_id=run_id, url=page.url, depth=page.depth, ok=True,
        assets_found=assets_after - assets_before,
        images_analyzed=analyzed, images_skipped=skipped,
    ))
    db.commit()


def _mark_run_error(run_id: int | None, message: str) -> None:
    """Cierra como ERROR un escaneo que nunca llegó a ejecutarse (validación fallida)."""
    if not run_id:
        return
    db = SessionLocal()
    try:
        run = db.get(ScanRun, run_id)
        if run is not None and run.status == "RUNNING":
            run.status = "ERROR"
            run.finished_at = datetime.utcnow()
            run.summary = f"Error: {message}"
            db.commit()
    finally:
        db.close()


async def run_market_scan(
    market_code: str,
    start_url: str | None = None,
    confirm_external_scope: bool = False,
    site_country: str | None = None,
    site_category: str | None = None,
    existing_run_id: int | None = None,
    use_saved_session: bool = True,
    use_login: bool = True,
) -> int:
    try:
        return await _run_market_scan(
            market_code, start_url, confirm_external_scope, site_country, site_category,
            existing_run_id, use_saved_session, use_login,
        )
    except ValueError as exc:
        # Validación de alcance rechazada antes de empezar: no dejar el run en RUNNING para siempre
        _mark_run_error(existing_run_id, str(exc))
        raise


async def _run_market_scan(
    market_code: str,
    start_url: str | None = None,
    confirm_external_scope: bool = False,
    site_country: str | None = None,
    site_category: str | None = None,
    existing_run_id: int | None = None,
    use_saved_session: bool = True,
    use_login: bool = True,
) -> int:
    markets = load_markets()
    scan_allowlist = []
    external_scope = bool(confirm_external_scope)
    configured_sites = []
    
    if start_url:
        parsed = urlsplit(start_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host:
            scan_allowlist = [host]
            if host.startswith("www."):
                scan_allowlist.append(host[4:])
            else:
                scan_allowlist.append("www." + host)
            scan_allowlist = list(dict.fromkeys([d for d in scan_allowlist if d]))
    else:
        if market_code not in markets:
            raise ValueError(f"Mercado '{market_code}' no existe en data/markets.json")
        cfg = markets[market_code]
        configured_sites = cfg["sites"]
        scan_allowlist = cfg["domain_allowlist"]
        start_urls = [site["url"] for site in configured_sites]
        start_url = start_urls[0] if start_urls else None
    if start_url and not configured_sites:
        start_urls = [start_url]
    
    parsed_site = urlsplit(start_url) if start_url else None
    site_url = f"{parsed_site.scheme.lower()}://{parsed_site.netloc.lower()}" if parsed_site else None

    db = SessionLocal()

    # Reutiliza el run creado por el endpoint; si no existe, crea uno nuevo
    run = db.get(ScanRun, existing_run_id) if existing_run_id else None
    if run is None:
        run = ScanRun(
            market=market_code,
            site_url=site_url,
            site_country=site_country,
            site_category=site_category,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    run_id = run.id  # se guarda ahora: tras cerrar la sesión el objeto queda desvinculado
    log.info("Escaneo #%s iniciado para %s", run_id, market_code)

    if not tesseract_available():
        log.error(
            "Tesseract no está instalado: las imágenes NO se leerán y aparecerán como «sin dato». "
            "Instálalo con tesseract (Windows: UB Mannheim, macOS: brew install tesseract tesseract-lang, "
            "Linux: tesseract-ocr). Configura TESSERACT_CMD en .env si no está en PATH."
        )


    registry = AssetRegistry()
    cancelled = False
    protected_pages = 0
    stopped_reason: str | None = None
    try:
        crawler = crawl_market(
            market_code, start_urls, scan_allowlist, run_id,
            allow_credentials=not external_scope, strict_public=external_scope,
            allow_saved_session=use_saved_session, allow_login=use_login,
        )
        # aclosing garantiza que el `finally` del crawler (cierre de contextos y
        # de Chromium) se ejecute también al cancelar o al romper el bucle.
        async with aclosing(crawler) as pages:
            async for page in pages:
                # Cancelación cooperativa: se comprueba antes de analizar cada página
                if cancellation.is_cancelled(run_id):
                    cancelled = True
                    log.info("Escaneo #%s cancelado por el usuario", run_id)
                    break

                if getattr(page, "auth_required", False):
                    # El crawler indica que se detuvo por no tener una sesión válida
                    if page.error and page.error.startswith(AUTH_STOPPED):
                        stopped_reason = page.error
                        log.warning("Escaneo #%s detenido: %s", run_id, stopped_reason)
                        break
                    # Página protegida: no es un fallo, es contenido que no se pudo auditar
                    protected_pages += 1
                    db.add(ScannedPage(
                        run_id=run_id, url=page.url, depth=page.depth,
                        ok=False, error=page.error or "Requiere autenticación",
                    ))
                    db.commit()
                    continue

                if not page.html:
                    # Página que no se pudo cargar: se registra con su motivo
                    run.pages_failed += 1
                    db.add(ScannedPage(
                        run_id=run_id, url=page.url, depth=page.depth,
                        ok=False, error=page.error or "Sin contenido",
                    ))
                    db.commit()
                    continue

                run.pages_scanned += 1
                # El análisis (OCR, PDFs, Veeva, BD) corre en un hilo: el servidor no se bloquea
                await asyncio.to_thread(_process_page, db, run, market_code, page, registry, scan_allowlist)
                log.info("Página %s analizada (%s)", run.pages_scanned, page.url)

        # Un solo aviso por escaneo (antes eran decenas de líneas, una por imagen)
        if registry.blocked_images:
            log.warning(
                "%s imagen(es) no se pudieron descargar (el sitio las rechaza con 403 fuera del navegador). "
                "Las que el navegador sí cargó se analizaron con normalidad.",
                registry.blocked_images,
            )

        run.assets_found = db.query(func.count(Asset.id)).filter(Asset.run_id == run_id).scalar() or 0
        stats = {
            row[0]: row[1]
            for row in db.query(Asset.status, func.count(Asset.id))
            .filter(Asset.run_id == run_id)
            .group_by(Asset.status)
        }
        stats.update({"pages": run.pages_scanned, "total": run.assets_found})

        if run.pages_scanned == 0 and not cancelled:
            # Un escaneo sin ninguna página leída no está «completado»: no se pudo auditar nada.
            # Se muestra el motivo real para que el usuario sepa qué corregir.
            last_error = (
                db.query(ScannedPage.error)
                .filter(ScannedPage.run_id == run_id, ScannedPage.ok.is_(False))
                .order_by(ScannedPage.id.desc())
                .limit(1)
                .scalar()
            )
            reason = (last_error or "ninguna página cargó")[:300]
            run.status = "ERROR"
            run.summary = (
                f"No se pudo leer ninguna página del sitio. Motivo: {reason}. "
                "Comprueba la conexión (VPN o red corporativa) y que el sitio abra en el navegador."
            )
        else:
            # Puede llamar a un LLM local (hasta 120 s): también fuera del event loop
            run.summary = await asyncio.to_thread(executive_summary, market_code, stats)
            # Lo detectado hasta el momento de cancelar se conserva
            run.status = "CANCELLED" if cancelled else "DONE"
            if cancelled:
                run.summary = f"Cancelado por el usuario. {run.summary}"
            # Aviso de cobertura: el informe debe decir la verdad sobre lo que no se pudo auditar
            if protected_pages:
                run.summary = (
                    f"{run.summary or ''} COBERTURA PARCIAL: {protected_pages} página(s) exigen iniciar sesión "
                    "y no se pudieron auditar."
                ).strip()
            if stopped_reason:
                run.summary = f"{run.summary or ''} ESCANEO DETENIDO: {stopped_reason}".strip()
    except Exception as exc:
        log.exception("Escaneo falló")
        db.rollback()
        run.status = "ERROR"
        detail = str(exc).strip() or type(exc).__name__
        run.summary = f"Error: {type(exc).__name__}: {detail}"
    finally:
        run.finished_at = datetime.utcnow()
        db.commit()
        db.close()
        cancellation.clear(run_id)
    return run_id
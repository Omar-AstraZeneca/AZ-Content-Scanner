import json
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Asset, ScanRun, ScannedPage
from app.scanner.cancellation import request_cancel
from app.scanner.crawler import is_allowed_url
from app.scanner.netguard import is_public_http_url_async
from app.scanner.pipeline import run_market_scan, load_markets

router = APIRouter(prefix="/api/scan", tags=["scan"])
DISPLAY_STATUSES = ("VENCIDO", "PROXIMO_A_VENCER", "REVISAR", "VIGENTE", "SIN_DATO")


def _status_counts_by_run(db: Session, run_ids: list[int]) -> dict[int, dict[str, int]]:
    counts = {run_id: {status: 0 for status in DISPLAY_STATUSES} for run_id in run_ids}
    if not run_ids:
        return counts
    rows = (
        db.query(Asset.run_id, Asset.status, func.count(Asset.id))
        .filter(Asset.run_id.in_(run_ids), Asset.status.in_(DISPLAY_STATUSES))
        .group_by(Asset.run_id, Asset.status)
        .all()
    )
    for run_id, status, count in rows:
        counts[run_id][status] = count
    return counts


def close_orphan_runs() -> int:
    """Cierra como ERROR los escaneos que quedaron RUNNING (por ejemplo, si el servidor se cayó).

    Debe llamarse una sola vez al arrancar el servidor: en ese momento ningún
    escaneo puede seguir en ejecución.
    """
    db = SessionLocal()
    try:
        orphans = db.query(ScanRun).filter(ScanRun.status == "RUNNING").all()
        for run in orphans:
            run.status = "ERROR"
            run.finished_at = datetime.utcnow()
            run.summary = "Interrumpido: el servidor se detuvo durante el escaneo"
        db.commit()
        return len(orphans)
    finally:
        db.close()


def _run_summary(run: ScanRun) -> dict:
    return {
        "id": run.id, "market": run.market, "status": run.status,
        "site_url": run.site_url,
        "site_country": run.site_country,
        "site_category": run.site_category,
        "started_at": run.started_at, "finished_at": run.finished_at,
        "pages_scanned": run.pages_scanned,
        "pages_failed": run.pages_failed,
        "assets_found": run.assets_found,
        "images_analyzed": run.images_analyzed,
        "images_skipped": run.images_skipped,
    }


class ScanStartIn(BaseModel):
    start_url: str | None = Field(None, max_length=2048)
    url: str | None = Field(None, max_length=2048)
    site_country: str | None = Field(None, max_length=80)
    site_category: str | None = Field(None, max_length=120)
    confirm_external_scope: bool = False
    # «Continuar sin logueo»: ignora la sesión guardada y no intenta ningún login.
    use_saved_session: bool = True
    use_login: bool = True


LOGIN_REQUIRED_CATEGORY_TOKENS = (
    "portal",
    "idaz",
    "programa de pacientes",
    "programa pacientes",
)


def _site_requires_login(site: dict) -> bool:
    """Marca los sitios que suelen tener secciones detrás del login.

    `requires_login` en data/markets.json tiene prioridad; si no existe, se
    infiere por categoría para no tener que editar el catálogo entero.
    """
    explicit = site.get("requires_login")
    if isinstance(explicit, bool):
        return explicit
    category = str(site.get("category") or "").lower()
    return any(token in category for token in LOGIN_REQUIRED_CATEGORY_TOKENS)


@router.get("/markets")
def list_markets():
    try:
        result = []
        for market in load_markets().values():
            result.append({
                "code": market["code"],
                "name": market["name"],
                "countries": market["countries"],
                "domain_allowlist": market["domain_allowlist"],
                "sites": [
                    {
                        **site,
                        "host": urlparse(site["url"]).hostname or "",
                        "requires_login": _site_requires_login(site),
                    }
                    for site in market["sites"]
                ],
            })
        return result
    except Exception:
        return []


@router.post("/start")
@router.post("/start/")
async def start_scan_simple(
    background: BackgroundTasks,
    body: ScanStartIn,
    db: Session = Depends(get_db),
):
    start_url = (body.url or body.start_url or "").strip()
    if not start_url:
        raise HTTPException(400, "URL requerida")
    try:
        parsed = urlparse(start_url)
        if parsed.scheme not in ["http", "https"]:
            raise ValueError("Protocolo no válido")
        if not parsed.hostname:
            raise ValueError("Hostname inválido")
    except Exception as e:
        raise HTTPException(400, f"URL inválida: {e}")

    already_running = db.query(ScanRun).filter(ScanRun.status == "RUNNING").first()
    if already_running:
        raise HTTPException(409, f"Ya hay un escaneo en curso (#{already_running.id}). Espera a que termine o cancélalo.")

    site_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    host = parsed.hostname.lower()
    market_code = host.replace(".", "_").upper()[:20]

    run = ScanRun(
        market=market_code,
        site_url=site_url,
        site_country=body.site_country,
        site_category=body.site_category,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    background.add_task(
        run_market_scan,
        market_code,
        start_url,
        False,
        body.site_country,
        body.site_category,
        run_id,
        use_saved_session=body.use_saved_session,
        use_login=body.use_login,
    )
    return {
        "message": f"Escaneo iniciado para {host}",
        "run_id": run_id,
        "site_url": site_url,
    }


@router.post("/start/{market_code}")
async def start_scan(
    market_code: str,
    background: BackgroundTasks,
    body: ScanStartIn | None = None,
    db: Session = Depends(get_db),
):
    # También soportar modo simplificado si viene URL directa
    start_url = (body.url or body.start_url).strip() if body and (body.url or body.start_url) else None
    if start_url and market_code.upper() in ["DIRECT", "CUSTOM", "URL"]:
        try:
            parsed = urlparse(start_url)
            if parsed.scheme not in ["http", "https"]:
                raise ValueError("Protocolo no válido")
            if not parsed.hostname:
                raise ValueError("Hostname inválido")
        except Exception as e:
            raise HTTPException(400, f"URL inválida: {e}")

        already_running = db.query(ScanRun).filter(ScanRun.status == "RUNNING").first()
        if already_running:
            raise HTTPException(409, f"Ya hay un escaneo en curso (#{already_running.id}). Espera a que termine o cancélalo.")

        site_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        host = parsed.hostname.lower()
        market_code = host.replace(".", "_").upper()[:20]

        run = ScanRun(
            market=market_code,
            site_url=site_url,
            site_country=body.site_country if body else None,
            site_category=body.site_category if body else None,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        background.add_task(
            run_market_scan,
            market_code,
            start_url,
            False,
            body.site_country if body else None,
            body.site_category if body else None,
            run_id,
            use_saved_session=body.use_saved_session if body else True,
            use_login=body.use_login if body else True,
        )
        return {
            "message": f"Escaneo iniciado para {host}",
            "run_id": run_id,
            "site_url": site_url,
        }

    markets = load_markets()
    if market_code not in markets:
        # Intentar modo directo
        if start_url:
            try:
                parsed = urlparse(start_url)
                if parsed.scheme not in ["http", "https"]:
                    raise ValueError("Protocolo no válido")
            except Exception as e:
                raise HTTPException(400, f"URL inválida: {e}")
            already_running = db.query(ScanRun).filter(ScanRun.status == "RUNNING").first()
            if already_running:
                raise HTTPException(409, f"Ya hay un escaneo en curso (#{already_running.id}). Espera a que termine o cancélalo.")

            site_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            host = parsed.hostname.lower()
            mcode = host.replace(".", "_").upper()[:20]

            run = ScanRun(
                market=mcode,
                site_url=site_url,
                site_country=body.site_country if body else None,
                site_category=body.site_category if body else None,
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id

            background.add_task(
                run_market_scan,
                mcode,
                start_url,
                False,
                body.site_country if body else None,
                body.site_category if body else None,
                run_id,
                use_saved_session=body.use_saved_session if body else True,
                use_login=body.use_login if body else True,
            )
            return {
                "message": f"Escaneo iniciado para {host}",
                "run_id": run_id,
                "site_url": site_url,
            }
        raise HTTPException(404, "Mercado no encontrado")
    start_url = body.start_url.strip() if body and body.start_url else None
    external = bool(start_url and not is_allowed_url(start_url, markets[market_code]["domain_allowlist"]))
    if external:
        if not (body and body.confirm_external_scope):
            raise HTTPException(400, "Confirma que el dominio externo está autorizado para este escaneo")
        try:
            if not await is_public_http_url_async(start_url):
                raise HTTPException(
                    400,
                    "URL rechazada: debe ser http(s) en puerto 80/443, sin usuario:contraseña, "
                    "y resolver a direcciones IP públicas.",
                )
        except Exception:
            pass  # Ser más permisivo
    if body and body.site_category and len(body.site_category) > 120:
        raise HTTPException(422, "La categoría del sitio no puede superar 120 caracteres")

    already_running = db.query(ScanRun).filter(ScanRun.status == "RUNNING").first()
    if already_running:
        raise HTTPException(409, f"Ya hay un escaneo en curso (#{already_running.id}). Espera a que termine o cancélalo.")

    site_country = body.site_country if body else None
    site_category = body.site_category if body else None
    parsed = urlparse(start_url) if start_url else None
    site_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed else None

    configured_sites = markets[market_code]["sites"]
    selected_site = next((site for site in configured_sites if site["url"] == start_url), None)
    run = ScanRun(
        market=market_code,
        site_url=site_url,
        site_country=site_country or (selected_site or {}).get("country"),
        site_category=site_category or (selected_site or {}).get("category"),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    background.add_task(
        run_market_scan,
        market_code,
        start_url,
        bool(external),
        site_country,
        site_category,
        run_id,
        use_saved_session=body.use_saved_session if body else True,
        use_login=body.use_login if body else True,
    )
    return {
        "message": f"Escaneo iniciado para {market_code}",
        "run_id": run_id,
        "site_url": site_url,
    }


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ScanRun, run_id)
    if run is None:
        raise HTTPException(404, "El escaneo no existe")
    if run.status != "RUNNING":
        raise HTTPException(409, "El escaneo ya no está en ejecución")
    request_cancel(run_id)
    return {"ok": True, "run_id": run_id, "status": "CANCELLING"}


@router.get("/runs")
def list_runs(
    market: str | None = None,
    site_url: str | None = None,
    site_country: str | None = None,
    site_category: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(ScanRun)
    if market:
        query = query.filter(ScanRun.market == market)
    if site_url:
        query = query.filter(ScanRun.site_url == site_url)
    if site_country:
        query = query.filter(ScanRun.site_country == site_country)
    if site_category:
        query = query.filter(ScanRun.site_category == site_category)
    runs = query.order_by(ScanRun.id.desc()).all()
    status_counts = _status_counts_by_run(db, [run.id for run in runs])
    return [{**_run_summary(run), "status_counts": status_counts[run.id]} for run in runs]


@router.get("/runs/{run_id}")
def get_run_detail(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ScanRun, run_id)
    if not run:
        raise HTTPException(404, "Escaneo no encontrado")

    counts = _status_counts_by_run(db, [run.id])[run.id]
    assets = (
        db.query(Asset)
        .filter(Asset.run_id == run.id, Asset.status.in_(DISPLAY_STATUSES))
        .order_by(Asset.id.asc())
        .all()
    )
    status_order = {status: index for index, status in enumerate(DISPLAY_STATUSES)}
    assets.sort(key=lambda asset: (status_order.get(asset.status, len(status_order)), asset.id))

    return {
        **_run_summary(run),
        "summary": run.summary,
        "status_counts": counts,
        "assets": [
            {
                "id": asset.id,
                "market": run.market,
                "site_country": run.site_country,
                "status": asset.status,
                "veeva_id": asset.veeva_id,
                "expiry_year": asset.expiry_year,
                "expiry_date": asset.expiry_date.isoformat() if asset.expiry_date else None,
                "page_url": asset.page_url,
                "asset_url": asset.asset_url,
                "asset_type": asset.asset_type,
                "recommendation": asset.recommendation,
                "occurrences": asset.occurrences or 1,
                "confidence": asset.confidence,
                "evidence": bool(asset.evidence_path),
            }
            for asset in assets
        ],
    }


@router.get("/runs/{run_id}/coverage")
def get_run_coverage(run_id: int, db: Session = Depends(get_db)):
    """Cobertura de un escaneo: qué páginas se visitaron, cuáles fallaron y por qué.

    Es la herramienta para saber si el escáner revisó todo: permite comparar dos
    escaneos página por página y ver el motivo de cada fallo.
    """
    run = db.get(ScanRun, run_id)
    if not run:
        raise HTTPException(404, "Escaneo no encontrado")

    pages = (
        db.query(ScannedPage)
        .filter(ScannedPage.run_id == run_id)
        .order_by(ScannedPage.id.asc())
        .all()
    )
    failed = [p for p in pages if not p.ok]
    return {
        "run_id": run_id,
        "status": run.status,
        "pages_visited": len(pages),
        "pages_ok": len(pages) - len(failed),
        "pages_failed": len(failed),
        "images_analyzed": run.images_analyzed,
        "images_skipped": run.images_skipped,
        "summary": run.summary,
        "pages": [
            {
                "url": p.url,
                "depth": p.depth,
                "ok": p.ok,
                "error": p.error,
                "assets_found": p.assets_found,
                "images_analyzed": p.images_analyzed,
                "images_skipped": p.images_skipped,
            }
            for p in pages
        ],
    }
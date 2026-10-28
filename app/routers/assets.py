import csv
import io
from datetime import date
from urllib.parse import urlsplit

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Asset, Validation, ManualMissed, ScanRun

router = APIRouter(prefix="/api/assets", tags=["assets"])

# Las capturas solo se sirven desde aquí (defensa ante una ruta manipulada en la BD)
EVIDENCE_ROOT = Path("data/evidence").resolve()


def _csv_safe(value):
    """Evita inyección de fórmulas al abrir el CSV en Excel/Sheets (=, +, -, @)."""
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _latest_run_ids(
    db: Session,
    site_url: str | None = None,
    site_country: str | None = None,
    site_category: str | None = None,
) -> list[int]:
    """Último escaneo terminado por región, sitio, país y categoría."""
    query = db.query(func.max(ScanRun.id)).filter(ScanRun.status == "DONE")
    if site_url:
        query = query.filter(ScanRun.site_url == site_url)
    if site_country:
        query = query.filter(ScanRun.site_country == site_country)
    if site_category:
        query = query.filter(ScanRun.site_category == site_category)
    sub = (
        query.group_by(ScanRun.market, ScanRun.site_url)
        .all()
    )
    return [r[0] for r in sub]


def _filtered(
    db, market, status, asset_type, year, search, only_latest,
    site_url=None, site_country=None, site_category=None,
):
    q = db.query(Asset)
    if only_latest:
        q = q.filter(Asset.run_id.in_(_latest_run_ids(db, site_url, site_country, site_category)))
    elif site_url or site_country or site_category:
        q = q.join(ScanRun, Asset.run_id == ScanRun.id)
        if site_url:
            q = q.filter(ScanRun.site_url == site_url)
        if site_country:
            q = q.filter(ScanRun.site_country == site_country)
        if site_category:
            q = q.filter(ScanRun.site_category == site_category)
    if market:
        q = q.filter(Asset.market == market)
    if status:
        q = q.filter(Asset.status == status)
    if asset_type:
        q = q.filter(Asset.asset_type == asset_type)
    if year:
        q = q.filter(Asset.expiry_year == year)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (Asset.page_url.ilike(like)) | (Asset.veeva_id.ilike(like)) | (Asset.asset_url.ilike(like))
        )
    return q


def _serialize(a: Asset) -> dict:
    return {
        "id": a.id, "market": a.market, "page_url": a.page_url, "asset_type": a.asset_type,
        "site_country": a.run.site_country, "site_category": a.run.site_category,
        "asset_url": a.asset_url, "snippet": a.snippet, "veeva_id": a.veeva_id,
        "expiry_year": a.expiry_year, "expiry_date": a.expiry_date.isoformat() if a.expiry_date else None,
        "status": a.status, "method": a.detection_method, "confidence": a.confidence,
        "occurrences": a.occurrences or 1,
        "recommendation": a.recommendation, "evidence": bool(a.evidence_path),
        "validated": a.validation is not None,
        "is_correct": a.validation.is_correct if a.validation else None,
        "veeva_detail": a.veeva_detail,
    }


@router.get("")
def list_assets(
    market: str | None = None, status: str | None = None, asset_type: str | None = None,
    year: int | None = None, search: str | None = None, site_url: str | None = None,
    site_country: str | None = None, site_category: str | None = None,
    only_latest: bool = True,
    limit: int = Query(500, le=5000), db: Session = Depends(get_db),
):
    order = {"VENCIDO": 0, "PROXIMO_A_VENCER": 1, "REVISAR": 2, "SIN_DATO": 3, "VIGENTE": 4}
    rows = _filtered(
        db, market, status, asset_type, year, search, only_latest,
        site_url, site_country, site_category,
    ).limit(limit).all()
    rows.sort(key=lambda a: (order.get(a.status, 9), a.expiry_year or 9999))
    return [_serialize(a) for a in rows]


@router.get("/summary")
def summary(
    market: str | None = None, site_url: str | None = None,
    site_country: str | None = None, site_category: str | None = None,
    status: str | None = None, asset_type: str | None = None,
    search: str | None = None,
    only_latest: bool = True, db: Session = Depends(get_db),
):
    q = db.query(Asset)
    if only_latest:
        q = q.filter(Asset.run_id.in_(_latest_run_ids(db, site_url, site_country, site_category)))
    elif site_url or site_country or site_category:
        q = q.join(ScanRun, Asset.run_id == ScanRun.id)
        if site_url:
            q = q.filter(ScanRun.site_url == site_url)
        if site_country:
            q = q.filter(ScanRun.site_country == site_country)
        if site_category:
            q = q.filter(ScanRun.site_category == site_category)
    if market:
        q = q.filter(Asset.market == market)
    if status:
        q = q.filter(Asset.status == status)
    if asset_type:
        q = q.filter(Asset.asset_type == asset_type)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (Asset.page_url.ilike(like)) | (Asset.veeva_id.ilike(like)) | (Asset.asset_url.ilike(like))
        )

    def group(col):
        return {k: v for k, v in q.with_entities(col, func.count(Asset.id)).group_by(col).all()}

    by_market_status = {}
    for m, s, c in q.with_entities(Asset.market, Asset.status, func.count(Asset.id)).group_by(Asset.market, Asset.status):
        by_market_status.setdefault(m, {})[s] = c

    runs = db.query(ScanRun).filter(
        ScanRun.status == "DONE",
        ScanRun.id.in_(_latest_run_ids(db, site_url, site_country, site_category)),
    )
    if market:
        runs = runs.filter(ScanRun.market == market)
    summaries = {
        f"{run.market} · {urlsplit(run.site_url).hostname}" if run.site_url else run.market: run.summary
        for run in runs.all()
    }

    return {
        "by_status": group(Asset.status),
        "by_type": group(Asset.asset_type),
        "by_year": {str(k): v for k, v in group(Asset.expiry_year).items() if k},
        "by_market_status": by_market_status,
        "summaries": summaries,
        "total": q.count(),
    }


@router.get("/export.csv")
def export_csv(
    market: str | None = None, status: str | None = None,
    site_url: str | None = None, site_country: str | None = None,
    site_category: str | None = None, db: Session = Depends(get_db),
):
    rows = _filtered(
        db, market, status, None, None, None, True,
        site_url, site_country, site_category,
    ).options(selectinload(Asset.extra_occurrences)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Mercado", "Tipo", "Estado", "Veeva ID", "Año venc.", "Fecha venc.", "Método",
                "Confianza", "URL página", "URL activo", "Apariciones (páginas)", "Otras páginas",
                "Recomendación"])
    for a in rows:
        others = " | ".join(o.page_url for o in a.extra_occurrences)
        w.writerow([_csv_safe(v) for v in (
            a.market, a.asset_type, a.status, a.veeva_id, a.expiry_year, a.expiry_date,
            a.detection_method, a.confidence, a.page_url, a.asset_url, a.occurrences or 1, others,
            a.recommendation,
        )])
    buf.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=reporte_{date.today()}.csv"},
    )


@router.get("/{asset_id}/evidence")
def evidence(asset_id: int, db: Session = Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a or not a.evidence_path:
        raise HTTPException(404, "Sin evidencia")
    path = Path(a.evidence_path).resolve()
    if EVIDENCE_ROOT not in path.parents or not path.is_file():
        raise HTTPException(404, "Sin evidencia")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{asset_id}/occurrences")
def occurrences(asset_id: int, db: Session = Depends(get_db)):
    """Todas las páginas donde aparece este activo (la primera es la de `page_url`)."""
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "Activo no encontrado")
    pages = [a.page_url] + [o.page_url for o in a.extra_occurrences]
    return {"asset_id": a.id, "count": len(pages), "pages": pages}


# ---------------------------- Validación manual (para métricas) ----------------------------
class ValidationIn(BaseModel):
    is_correct: bool
    corrected_year: int | None = Field(None, ge=2000, le=2100)
    corrected_veeva_id: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=2000)
    reviewed_by: str | None = Field(None, max_length=80)


def _reviewer(request: Request, provided: str | None) -> str | None:
    """Con autenticación activa el revisor es el usuario autenticado (no se puede suplantar)."""
    return getattr(request.state, "user", None) or provided


@router.post("/{asset_id}/validate")
def validate(asset_id: int, body: ValidationIn, request: Request, db: Session = Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "Activo no encontrado")
    v = a.validation or Validation(asset_id=asset_id, is_correct=body.is_correct)
    v.is_correct = body.is_correct
    v.corrected_year = body.corrected_year
    v.corrected_veeva_id = body.corrected_veeva_id
    v.notes = body.notes
    v.reviewed_by = _reviewer(request, body.reviewed_by)
    db.add(v)
    db.commit()
    return {"ok": True}


class MissedIn(BaseModel):
    market: str = Field(max_length=10)
    page_url: str = Field(max_length=2048)
    asset_type: str = Field(max_length=20)
    description: str | None = Field(None, max_length=2000)
    reviewed_by: str | None = Field(None, max_length=80)


@router.post("/missed")
def add_missed(body: MissedIn, request: Request, db: Session = Depends(get_db)):
    data = body.model_dump()
    data["reviewed_by"] = _reviewer(request, body.reviewed_by)
    db.add(ManualMissed(**data))
    db.commit()
    return {"ok": True}
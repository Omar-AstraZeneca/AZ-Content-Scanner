import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.metrics.pilot_metrics import compute_pilot_metrics
from app.models import ScanRun

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/pilot")
def pilot_metrics(
    market: str | None = None,
    site_url: str | None = None,
    site_country: str | None = None,
    site_category: str | None = None,
    db: Session = Depends(get_db),
):
    return compute_pilot_metrics(db, market, site_url, site_country, site_category)


@router.get("/pilot/export.csv")
def export_pilot_metrics_csv(
    market: str | None = None,
    site_url: str | None = None,
    site_country: str | None = None,
    site_category: str | None = None,
    db: Session = Depends(get_db),
):
    metrics = compute_pilot_metrics(db, market, site_url, site_country, site_category)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["métrica", "valor"])
    writer.writerow(["scope", metrics.get("scope", "TODOS")])
    writer.writerow(["runs", metrics.get("runs", 0)])
    writer.writerow(["pages_scanned", metrics.get("pages_scanned", 0)])
    writer.writerow(["pages_failed", metrics.get("pages_failed", 0)])
    writer.writerow(["assets_detected", metrics.get("assets_detected", 0)])
    writer.writerow(["precision", metrics.get("validation", {}).get("precision")])
    writer.writerow(["recall", metrics.get("validation", {}).get("recall")])
    writer.writerow(["f1", metrics.get("validation", {}).get("f1")])
    writer.writerow(["reviewed", metrics.get("validation", {}).get("reviewed")])
    writer.writerow(["hours_saved", metrics.get("time", {}).get("hours_saved")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=metricas_{date.today()}.csv"},
    )


@router.get("/pilot/{run_id}")
def pilot_metrics_by_run(
    run_id: int,
    db: Session = Depends(get_db),
):
    run = db.get(ScanRun, run_id)
    if not run:
        raise HTTPException(404, "Escaneo no encontrado")
    return {
        "id": run.id,
        "market": run.market,
        "site_url": run.site_url,
        "site_country": run.site_country,
        "site_category": run.site_category,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "pages_scanned": run.pages_scanned,
        "pages_failed": run.pages_failed,
        "assets_found": run.assets_found,
        "summary": run.summary,
    }
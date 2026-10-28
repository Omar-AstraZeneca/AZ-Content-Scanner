"""
Métricas del piloto (con datos propios):

  - Páginas escaneadas / fallidas
  - Activos detectados (por tipo y estado)
  - Precisión, Recall y F1 vs. revisión manual (verdad de terreno)
  - Tiempo de escaneo vs. tiempo manual estimado -> horas ahorradas

Definiciones:
  TP = detecciones validadas como correctas
  FP = detecciones validadas como incorrectas
  FN = activos que la revisión manual encontró y el escáner NO (tabla manual_missed)
  Precisión = TP / (TP + FP)
  Recall    = TP / (TP + FN)
  F1        = 2 * P * R / (P + R)
"""
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from urllib.parse import urlsplit

from app.config import get_settings
from app.models import ScanRun, Asset, Validation, ManualMissed

settings = get_settings()


def _safe_div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def _f1_score(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return _safe_div(2 * precision * recall, precision + recall)


def compute_pilot_metrics(
    db: Session,
    market: str | None = None,
    site_url: str | None = None,
    site_country: str | None = None,
    site_category: str | None = None,
) -> dict:
    runs_q = db.query(ScanRun).filter(ScanRun.status == "DONE")
    assets_q = db.query(Asset)
    if market:
        runs_q = runs_q.filter(ScanRun.market == market)
        assets_q = assets_q.filter(Asset.market == market)
    if site_url:
        runs_q = runs_q.filter(ScanRun.site_url == site_url)
    if site_country:
        runs_q = runs_q.filter(ScanRun.site_country == site_country)
    if site_category:
        runs_q = runs_q.filter(ScanRun.site_category == site_category)
    if site_url or site_country or site_category:
        assets_q = assets_q.join(ScanRun, Asset.run_id == ScanRun.id)
        if site_url:
            assets_q = assets_q.filter(ScanRun.site_url == site_url)
        if site_country:
            assets_q = assets_q.filter(ScanRun.site_country == site_country)
        if site_category:
            assets_q = assets_q.filter(ScanRun.site_category == site_category)

    runs = runs_q.all()
    pages_scanned = sum(r.pages_scanned for r in runs)
    pages_failed = sum(r.pages_failed for r in runs)

    # Tiempo real de escaneo (segundos)
    scan_seconds = sum((r.finished_at - r.started_at).total_seconds() for r in runs if r.finished_at)
    scan_hours = scan_seconds / 3600

    # Tiempo manual estimado
    manual_hours = (pages_scanned * settings.manual_minutes_per_page) / 60
    hours_saved = max(manual_hours - scan_hours, 0)
    reduction_pct = _safe_div(hours_saved, manual_hours)

    # Activos por tipo y estado
    by_type = {t: c for t, c in assets_q.with_entities(Asset.asset_type, func.count(Asset.id)).group_by(Asset.asset_type)}
    by_status = {s: c for s, c in assets_q.with_entities(Asset.status, func.count(Asset.id)).group_by(Asset.status)}
    by_method = {m: c for m, c in assets_q.with_entities(Asset.detection_method, func.count(Asset.id)).group_by(Asset.detection_method)}
    total_assets = sum(by_type.values())

    # Precisión vs. revisión manual
    val_q = db.query(Validation).join(Asset, Asset.id == Validation.asset_id)
    if site_url or site_country or site_category:
        val_q = val_q.join(ScanRun, Asset.run_id == ScanRun.id)
        if site_url:
            val_q = val_q.filter(ScanRun.site_url == site_url)
        if site_country:
            val_q = val_q.filter(ScanRun.site_country == site_country)
        if site_category:
            val_q = val_q.filter(ScanRun.site_category == site_category)
    if market:
        val_q = val_q.filter(Asset.market == market)
    validations = val_q.all()
    tp = sum(1 for v in validations if v.is_correct)
    fp = sum(1 for v in validations if not v.is_correct)

    fn_q = db.query(func.count(ManualMissed.id))
    if market:
        fn_q = fn_q.filter(ManualMissed.market == market)
    if site_url:
        parsed = urlsplit(site_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        fn_q = fn_q.filter(or_(ManualMissed.page_url == origin, ManualMissed.page_url.like(origin + "/%")))
    fn = fn_q.scalar() or 0

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _f1_score(precision, recall)

    # Precisión por método (para comparar OCR vs. regex vs. VLM)
    method_precision = {}
    for method in by_method:
        sub = [v for v in validations if v.asset.detection_method == method]
        if sub:
            method_precision[method] = _safe_div(sum(1 for v in sub if v.is_correct), len(sub))

    # Cobertura de la validación
    validated_pct = _safe_div(len(validations), total_assets)

    return {
        "scope": market or "TODOS",
        "runs": len(runs),
        "pages_scanned": pages_scanned,
        "pages_failed": pages_failed,
        "assets_detected": total_assets,
        "assets_by_type": by_type,
        "assets_by_status": by_status,
        "assets_by_method": by_method,
        "validation": {
            "reviewed": len(validations),
            "reviewed_pct": validated_pct,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "precision_by_method": method_precision,
        },
        "time": {
            "scan_hours": round(scan_hours, 3),
            "manual_hours_estimated": round(manual_hours, 2),
            "hours_saved": round(hours_saved, 2),
            "reduction_pct": reduction_pct,
            "manual_minutes_per_page": settings.manual_minutes_per_page,
        },
    }
"""
Reclasifica los estados de los activos ya guardados con la regla vigente
(fecha de hoy + ventana de 60 días), sin volver a escanear.

Uso:  python -m scripts.reclassify
"""
from app.database import SessionLocal, init_db
from app.models import Asset
from app.scanner.extractors import classify_status


def main() -> None:
    init_db()
    db = SessionLocal()
    changed = 0
    try:
        for asset in db.query(Asset).all():
            if asset.status == "REVISAR":
                continue  # depende de la confianza de la detección, no de la fecha de hoy
            new_status = classify_status(asset.expiry_year, asset.expiry_date)
            if new_status != asset.status:
                asset.status = new_status
                changed += 1
        db.commit()
    finally:
        db.close()
    print(f"Activos reclasificados: {changed}")


if __name__ == "__main__":
    main()
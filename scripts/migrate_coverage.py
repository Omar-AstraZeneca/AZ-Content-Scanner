"""
Migración: añade columnas de cobertura a scan_runs y asegura las que usa el historial.
Uso:  python -m scripts.migrate_coverage
Es seguro ejecutarlo varias veces. La tabla scanned_pages se crea sola al arrancar.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/scanner.db")

NEW_COLUMNS = {
    "images_analyzed": "INTEGER DEFAULT 0",
    "images_skipped": "INTEGER DEFAULT 0",
    "site_url": "TEXT",
    "site_country": "VARCHAR(80)",
    "site_category": "VARCHAR(120)",
}


def main() -> None:
    if not DB_PATH.exists():
        print("No existe data/scanner.db: se creará al arrancar el servidor.")
        return
    con = sqlite3.connect(DB_PATH)
    try:
        existing = {row[1] for row in con.execute("PRAGMA table_info(scan_runs)")}
        for name, ddl in NEW_COLUMNS.items():
            if name not in existing:
                con.execute(f"ALTER TABLE scan_runs ADD COLUMN {name} {ddl}")
                print(f"Columna añadida: scan_runs.{name}")
        con.commit()
    finally:
        con.close()
    print("Migración completada.")


if __name__ == "__main__":
    main()
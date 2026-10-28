from pathlib import Path
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import get_settings

settings = get_settings()

Path("data/evidence").mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        """El escaneo escribe desde un hilo mientras la API lee desde otro: WAL evita que
        se bloqueen entre sí y busy_timeout espera en lugar de fallar con «database is locked»."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def backfill_missing_scan_runs() -> None:
    with engine.begin() as connection:
        if connection.execute(text("SELECT COUNT(*) FROM scan_runs")).scalar() > 0:
            return

        run_ids = connection.execute(
            text("SELECT DISTINCT run_id FROM assets WHERE run_id IS NOT NULL ORDER BY run_id")
        ).fetchall()
        for (run_id,) in run_ids:
            if connection.execute(text("SELECT 1 FROM scan_runs WHERE id = :run_id"), {"run_id": run_id}).scalar():
                continue
            row = connection.execute(
                text(
                    "SELECT market, MIN(scanned_at), MAX(scanned_at), COUNT(DISTINCT page_url), COUNT(*) "
                    "FROM assets WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ).fetchone()
            if not row:
                continue
            market, started_at, finished_at, pages_scanned, assets_found = row
            sample_url = connection.execute(
                text("SELECT page_url FROM assets WHERE run_id = :run_id ORDER BY scanned_at LIMIT 1"),
                {"run_id": run_id},
            ).scalar()
            connection.execute(
                text(
                    "INSERT INTO scan_runs (id, market, site_url, site_country, site_category, started_at, finished_at, status, pages_scanned, pages_failed, assets_found, images_analyzed, images_skipped, summary) "
                    "VALUES (:id, :market, :site_url, NULL, NULL, :started_at, :finished_at, 'DONE', :pages_scanned, 0, :assets_found, 0, 0, 'Reconstructed from historical asset data')"
                ),
                {
                    "id": run_id,
                    "market": market,
                    "site_url": sample_url,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "pages_scanned": pages_scanned,
                    "assets_found": assets_found,
                },
            )


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_columns = {column["name"] for column in inspect(engine).get_columns("scan_runs")}
    additions = {
        "site_url": "TEXT",
        "site_country": "VARCHAR(80)",
        "site_category": "VARCHAR(120)",
    }
    asset_columns = {column["name"] for column in inspect(engine).get_columns("assets")}
    with engine.begin() as connection:
        for column, sql_type in additions.items():
            if column not in run_columns:
                connection.execute(text(f"ALTER TABLE scan_runs ADD COLUMN {column} {sql_type}"))
        if "occurrences" not in asset_columns:
            connection.execute(text("ALTER TABLE assets ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1"))

        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS app_migrations "
            "(name TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        ))
        migrated = connection.execute(
            text("SELECT 1 FROM app_migrations WHERE name = 'regional-sites-v1'")
        ).scalar()
        if not migrated:
            for old_market, region, country, site_url, category in (
                ("CO", "ANDINO", "Colombia", None, None),
                ("PE", "ANDINO", "Perú", None, None),
                ("CR", "CAMCAR", "Costa Rica", "https://www.azmed.cr", "Portal HCP"),
            ):
                connection.execute(
                    text(
                        "UPDATE scan_runs SET market = :region, "
                        "site_url = COALESCE(site_url, :site_url), "
                        "site_country = COALESCE(site_country, :country), "
                        "site_category = COALESCE(site_category, :category) "
                        "WHERE market = :old_market"
                    ),
                    {
                        "region": region,
                        "site_url": site_url,
                        "country": country,
                        "category": category,
                        "old_market": old_market,
                    },
                )
                connection.execute(
                    text("UPDATE assets SET market = :region WHERE market = :old_market"),
                    {"region": region, "old_market": old_market},
                )
                connection.execute(
                    text("UPDATE manual_missed SET market = :region WHERE market = :old_market"),
                    {"region": region, "old_market": old_market},
                )

            connection.execute(
                text("INSERT INTO app_migrations (name) VALUES ('regional-sites-v1')")
            )

        legacy_sites_migrated = connection.execute(
            text("SELECT 1 FROM app_migrations WHERE name = 'legacy-camcar-sites-v2'")
        ).scalar()
        if not legacy_sites_migrated:
            connection.execute(text(
                "UPDATE scan_runs SET site_url = 'https://www.azmed.cr', "
                "site_country = 'Costa Rica', site_category = 'Portal HCP' "
                "WHERE market = 'CAMCAR' AND site_url IS NULL AND status = 'DONE'"
            ))
            connection.execute(text(
                "UPDATE scan_runs SET site_country = 'Costa Rica', site_category = 'Portal HCP' "
                "WHERE market = 'CAMCAR' AND site_url LIKE '%azmed.cr%' AND site_category IS NULL"
            ))
            connection.execute(
                text("INSERT INTO app_migrations (name) VALUES ('legacy-camcar-sites-v2')")
            )

    backfill_missing_scan_runs()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
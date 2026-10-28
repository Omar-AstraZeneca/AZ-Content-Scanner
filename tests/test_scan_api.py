import json
from datetime import datetime

from fastapi.testclient import TestClient

from app.database import SessionLocal, backfill_missing_scan_runs
from app.main import app
from app.models import Asset, ScanRun
from app.routers import scan as scan_router


def test_markets_include_all_configured_sites():
    with TestClient(app) as client:
        response = client.get("/api/scan/markets")

    assert response.status_code == 200
    markets = {market["code"]: market for market in response.json()}
    with open("data/markets.json", encoding="utf-8") as f:
        catalog = {m["code"]: m for m in json.load(f)["markets"]}
    assert set(markets) == set(catalog) and len(markets) == 8
    assert markets["ANDINO"]["countries"] == ["Colombia", "Ecuador", "Perú"]
    for code, market in catalog.items():
        assert {site["host"] for site in markets[code]["sites"]} == {
            site["url"].split("/")[2] for site in market["sites"]
        }


def test_scan_starts_selected_url_only_for_allowed_market(monkeypatch):
    started = []

    def fake_run_market_scan(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(scan_router, "run_market_scan", fake_run_market_scan)

    with TestClient(app) as client:
        response = client.post(
            "/api/scan/start/CAMCAR",
            json={"start_url": "https://www.azmed.cr/camcar"},
        )

    assert response.status_code == 200
    assert response.json()["site_url"] == "https://www.azmed.cr"
    assert started == [(
        ("CAMCAR", "https://www.azmed.cr/camcar", False, None, None, response.json()["run_id"]),
        {"use_saved_session": True, "use_login": True},
    )]


def test_scan_without_login_disables_saved_session_and_login(monkeypatch):
    started = []

    def fake_run_market_scan(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(scan_router, "run_market_scan", fake_run_market_scan)

    with TestClient(app) as client:
        response = client.post(
            "/api/scan/start/CAMCAR",
            json={
                "start_url": "https://www.azmed.cr/",
                "use_saved_session": False,
                "use_login": False,
            },
        )

    assert response.status_code == 200
    assert started[0][1] == {"use_saved_session": False, "use_login": False}


def test_markets_flag_sites_that_require_login():
    with TestClient(app) as client:
        response = client.get("/api/scan/markets")

    assert response.status_code == 200
    markets = {market["code"]: market for market in response.json()}
    sites = {site["url"]: site for market in markets.values() for site in market["sites"]}
    assert sites["https://www.azmed.cr/"]["requires_login"] is True
    assert sites["https://www.astrazeneca.com/"]["requires_login"] is False


def test_scan_requires_confirmation_for_unlisted_public_domain():
    with TestClient(app) as client:
        response = client.post(
            "/api/scan/start/ANDINO",
            json={"start_url": "https://hcp-content.example.com/"},
        )

    assert response.status_code == 400


def test_scan_accepts_confirmed_public_domain(monkeypatch, fake_dns):
    fake_dns("hcp-content.example.com", "93.184.216.34")
    started = []
    monkeypatch.setattr(
        scan_router,
        "run_market_scan",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/scan/start/ANDINO",
            json={
                "start_url": "https://hcp-content.example.com/",
                "site_country": "Ecuador",
                "site_category": "Portal HCP",
                "confirm_external_scope": True,
            },
        )

    assert response.status_code == 200
    assert started == [(
        ("ANDINO", "https://hcp-content.example.com/", True, "Ecuador", "Portal HCP", response.json()["run_id"]),
        {"use_saved_session": True, "use_login": True},
    )]


def test_scan_rejects_private_host_even_with_confirmation():
    with TestClient(app) as client:
        response = client.post(
            "/api/scan/start/CAMCAR",
            json={
                "start_url": "http://127.0.0.1:8000/",
                "confirm_external_scope": True,
            },
        )

    assert response.status_code == 400


def test_scan_history_returns_all_runs_without_limit():
    db = SessionLocal()
    try:
        db.query(ScanRun).delete()
        for idx in range(55):
            db.add(ScanRun(
                market="MX",
                site_url=f"https://site{idx}.example.com",
                site_country="México",
                site_category="Portal HCP",
                status="DONE" if idx % 2 == 0 else "RUNNING",
                pages_scanned=idx,
                pages_failed=0,
                assets_found=idx,
            ))
        db.commit()

        with TestClient(app) as client:
            response = client.get("/api/scan/runs")

        assert response.status_code == 200
        assert len(response.json()) == 55
    finally:
        db.query(ScanRun).delete()
        db.commit()
        db.close()


def test_scan_history_backfills_missing_run_rows_from_assets():
    db = SessionLocal()
    try:
        db.query(Asset).delete()
        db.query(ScanRun).delete()
        db.add(Asset(
            id=101,
            run_id=42,
            market="MX",
            page_url="https://www.azmed.com.mx",
            asset_type="texto",
            asset_url="https://www.azmed.com.mx/asset",
            snippet="Material vencido 2025",
            expiry_year=2025,
            status="VENCIDO",
            detection_method="regex",
            confidence=0.95,
            scanned_at=datetime.utcnow(),
        ))
        db.commit()
        backfill_missing_scan_runs()

        with TestClient(app) as client:
            response = client.get("/api/scan/runs")

        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["market"] == "MX"
        assert response.json()[0]["site_url"] == "https://www.azmed.com.mx"
        assert response.json()[0]["status_counts"] == {
            "VENCIDO": 1,
            "PROXIMO_A_VENCER": 0,
            "REVISAR": 0,
            "VIGENTE": 0,
            "SIN_DATO": 0,
        }
    finally:
        db.query(Asset).delete()
        db.query(ScanRun).delete()
        db.commit()
        db.close()


def test_scan_run_detail_returns_status_counts_and_result_urls():
    db = SessionLocal()
    try:
        db.query(Asset).delete()
        db.query(ScanRun).delete()
        run = ScanRun(
            market="MX",
            site_url="https://www.azmed.com.mx",
            site_country="México",
            site_category="Portal HCP",
            status="DONE",
        )
        db.add(run)
        db.flush()
        for status, expiry_year in [
            ("VENCIDO", 2024),
            ("PROXIMO_A_VENCER", 2026),
            ("VIGENTE", 2028),
            ("SIN_DATO", None),
        ]:
            db.add(Asset(
                run_id=run.id,
                market="MX",
                page_url=f"https://www.azmed.com.mx/{status.lower()}",
                asset_type="texto",
                asset_url=f"https://www.azmed.com.mx/content/{status.lower()}",
                veeva_id=f"MX-{status}",
                expiry_year=expiry_year,
                status=status,
                detection_method="regex",
                confidence=0.9,
            ))
        db.commit()
        run_id = run.id

        with TestClient(app) as client:
            response = client.get(f"/api/scan/runs/{run_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status_counts"] == {
            "VENCIDO": 1,
            "PROXIMO_A_VENCER": 1,
            "REVISAR": 0,
            "VIGENTE": 1,
            "SIN_DATO": 1,
        }
        assert {asset["status"] for asset in payload["assets"]} == {
            "VENCIDO",
            "PROXIMO_A_VENCER",
            "VIGENTE",
            "SIN_DATO",
        }
        assert {asset["page_url"] for asset in payload["assets"]} == {
            "https://www.azmed.com.mx/vencido",
            "https://www.azmed.com.mx/proximo_a_vencer",
            "https://www.azmed.com.mx/vigente",
            "https://www.azmed.com.mx/sin_dato",
        }
    finally:
        db.query(Asset).delete()
        db.query(ScanRun).delete()
        db.commit()
        db.close()
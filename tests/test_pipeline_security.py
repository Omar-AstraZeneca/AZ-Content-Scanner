import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Asset, AssetOccurrence, ScanRun, ScannedPage
from app.scanner import pipeline
from app.scanner.crawler import PageResult
from app.scanner.pipeline import AssetRegistry, _analyze_images, run_market_scan

ALLOW = ["azmed.com.mx"]
IMG = '<img src="/img/banner.png" alt="Approval ID: MX-12345 Vence 2024">'


def page(url, html="<html><body>texto</body></html>"):
    return PageResult(url=url, html=html, visible_text="", footer_text="", screenshot_path=None,
                      links=[], pdf_links=[])


@pytest.fixture
def db():
    session = SessionLocal()
    _clean(session)
    yield session
    _clean(session)
    session.close()


def _clean(session):
    for model in (AssetOccurrence, ScannedPage, Asset, ScanRun):
        session.query(model).delete()
    session.commit()


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(pipeline, "download_image", lambda *a, **k: None)


# ------------------------------------------------------------------ apariciones (punto 6)
def test_same_asset_on_many_pages_is_recorded_once_with_all_pages(db, no_network):
    run = ScanRun(market="MX"); db.add(run); db.commit()
    registry = AssetRegistry()

    for n in range(4):
        _analyze_images(db, run, "MX", page(f"https://www.azmed.com.mx/p{n}", IMG), registry, ALLOW)
    db.commit()

    assets = db.query(Asset).all()
    assert len(assets) == 1                                   # un solo activo (no infla los KPI)
    assert assets[0].page_url == "https://www.azmed.com.mx/p0"
    assert assets[0].occurrences == 4
    assert {o.page_url for o in assets[0].extra_occurrences} == {f"https://www.azmed.com.mx/p{n}" for n in (1, 2, 3)}


def test_same_page_twice_does_not_duplicate_occurrence(db, no_network):
    run = ScanRun(market="MX"); db.add(run); db.commit()
    registry = AssetRegistry()
    for _ in range(3):
        _analyze_images(db, run, "MX", page("https://www.azmed.com.mx/a", IMG), registry, ALLOW)
    _analyze_images(db, run, "MX", page("https://www.azmed.com.mx/b", IMG), registry, ALLOW)
    _analyze_images(db, run, "MX", page("https://www.azmed.com.mx/b", IMG), registry, ALLOW)
    db.commit()
    asset = db.query(Asset).one()
    assert asset.occurrences == 2 and db.query(AssetOccurrence).count() == 1


def test_asset_without_data_creates_no_phantom_occurrences(db, no_network):
    run = ScanRun(market="MX"); db.add(run); db.commit()
    registry = AssetRegistry()
    plain = '<img src="/img/sin-datos.png" alt="foto">'
    for n in range(3):
        _analyze_images(db, run, "MX", page(f"https://www.azmed.com.mx/p{n}", plain), registry, ALLOW)
    db.commit()
    assert db.query(Asset).count() == 0 and db.query(AssetOccurrence).count() == 0


def test_occurrences_endpoint_and_run_detail(db, no_network):
    run = ScanRun(market="MX", status="DONE"); db.add(run); db.commit()
    registry = AssetRegistry()
    for n in range(3):
        _analyze_images(db, run, "MX", page(f"https://www.azmed.com.mx/p{n}", IMG), registry, ALLOW)
    db.commit()
    asset_id = db.query(Asset).one().id
    run_id = run.id

    with TestClient(app) as client:
        data = client.get(f"/api/assets/{asset_id}/occurrences").json()
        assert data["count"] == 3
        assert data["pages"][0] == "https://www.azmed.com.mx/p0"
        detail = client.get(f"/api/scan/runs/{run_id}").json()
        assert detail["assets"][0]["occurrences"] == 3
        csv_text = client.get("/api/assets/export.csv?market=MX").text
        assert "Apariciones" in csv_text and "https://www.azmed.com.mx/p2" in csv_text
        assert client.get("/api/assets/999999/occurrences").status_code == 404


def test_non_http_video_sources_are_ignored(db):
    run = ScanRun(market="MX"); db.add(run); db.commit()
    html = '<iframe src="javascript:alert(1)//youtube"></iframe><iframe src="https://www.youtube.com/embed/x"></iframe>'
    pipeline._analyze_videos(db, run, "MX", page("https://www.azmed.com.mx/v", html), AssetRegistry())
    db.commit()
    assert [a.asset_url for a in db.query(Asset).all()] == ["https://www.youtube.com/embed/x"]


# ------------------------------------------------------------------ el servidor no se bloquea (punto 5)
def _fake_crawl(captured=None, pages=2):
    async def crawl(market_code, start_urls, allowlist, run_id, **kwargs):
        if captured is not None:
            captured.update(kwargs, allowlist=allowlist)
        for n in range(pages):
            yield page(f"https://www.azmed.com.mx/p{n}")
    return crawl


def test_event_loop_stays_responsive_while_a_page_is_analyzed(db, monkeypatch):
    monkeypatch.setattr(pipeline, "crawl_market", _fake_crawl(pages=2))
    original = pipeline._process_page

    def slow(*args):
        time.sleep(0.4)                 # simula OCR / PDF / Veeva
        return original(*args)

    monkeypatch.setattr(pipeline, "_process_page", slow)

    async def main():
        ticks = 0

        async def ticker():              # representa al dashboard / botón Cancelar pidiendo cosas
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        started = time.monotonic()
        run_id = await run_market_scan("MX")
        elapsed = time.monotonic() - started
        task.cancel()
        return run_id, ticks, elapsed

    run_id, ticks, elapsed = asyncio.run(main())
    assert elapsed >= 0.8                # el análisis lento sí ocurrió (2 páginas x 0.4 s)
    assert ticks >= 8, f"el event loop se bloqueó: solo {ticks} ticks en {elapsed:.2f}s"
    run = db.get(ScanRun, run_id)
    assert run.status == "DONE" and run.pages_scanned == 2


# ------------------------------------------------------------------ alcance y credenciales (7 y 8)
def test_configured_site_scan_may_use_credentials(db, monkeypatch):
    captured = {}
    monkeypatch.setattr(pipeline, "crawl_market", _fake_crawl(captured, pages=1))
    asyncio.run(run_market_scan("MX", "https://www.azmed.com.mx/"))
    assert captured["allow_credentials"] is True and captured["strict_public"] is False


def test_external_scan_gets_no_credentials_and_strict_ip_checks(db, monkeypatch, fake_dns):
    fake_dns("hcp-externo.example.com", "93.184.216.34")
    captured = {}
    monkeypatch.setattr(pipeline, "crawl_market", _fake_crawl(captured, pages=1))
    asyncio.run(run_market_scan("MX", "https://hcp-externo.example.com/", confirm_external_scope=True))
    assert captured["allow_credentials"] is False       # nada de contraseñas a un dominio externo
    assert captured["strict_public"] is True
    assert captured["allowlist"] == ["hcp-externo.example.com"]


def test_external_scan_to_internal_host_is_refused_and_run_closed(db, monkeypatch, fake_dns):
    fake_dns("looks-public.example.com", "10.9.9.9")
    run = ScanRun(market="MX", status="RUNNING"); db.add(run); db.commit(); run_id = run.id
    monkeypatch.setattr(pipeline, "crawl_market", _fake_crawl(pages=1))

    with pytest.raises(ValueError):
        asyncio.run(run_market_scan("MX", "https://looks-public.example.com/", True, existing_run_id=run_id))

    db.expire_all()
    closed = db.get(ScanRun, run_id)
    assert closed.status == "ERROR" and closed.finished_at is not None   # antes quedaba «RUNNING» para siempre


def test_external_scan_without_confirmation_is_refused(db, monkeypatch, fake_dns):
    fake_dns("hcp-externo.example.com", "93.184.216.34")
    monkeypatch.setattr(pipeline, "crawl_market", _fake_crawl(pages=1))
    with pytest.raises(ValueError):
        asyncio.run(run_market_scan("MX", "https://hcp-externo.example.com/", confirm_external_scope=False))

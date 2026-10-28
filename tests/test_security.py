import base64

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Asset, ScanRun, Validation


def basic(user="admin", password="s3cret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def password(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    security.reset_rate_limits()
    yield "s3cret"
    security.reset_rate_limits()


# ------------------------------------------------------------------ sin contraseña: solo local
def test_without_password_local_access_works(client):
    assert client.get("/api/scan/markets").status_code == 200


def test_without_password_remote_ip_is_rejected():
    with TestClient(app, client=("203.0.113.9", 50000)) as remote:
        response = remote.get("/api/scan/markets")
    assert response.status_code == 403
    assert "DASHBOARD_PASSWORD" in response.json()["detail"]


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host"])
def test_without_password_proxy_headers_are_rejected(client, header):
    # Un proxy inverso en la misma máquina haría que todos parecieran «locales»
    assert client.get("/api/scan/markets", headers={header: "198.51.100.7"}).status_code == 403


# ------------------------------------------------------------------ Host (DNS rebinding)
def test_unknown_host_header_is_rejected(client):
    assert client.get("/api/scan/markets", headers={"Host": "evil.attacker.com"}).status_code == 400


def test_known_host_with_port_is_accepted(client):
    assert client.get("/api/scan/markets", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/api/scan/markets", headers={"Host": "[::1]:8000"}).status_code == 200


def test_allowed_hosts_is_configurable(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "allowed_hosts", "scanner.azcorp.com")
    assert client.get("/api/scan/markets", headers={"Host": "scanner.azcorp.com"}).status_code == 200
    assert client.get("/api/scan/markets", headers={"Host": "localhost"}).status_code == 400


# ------------------------------------------------------------------ autenticación
def test_password_required(client, password):
    response = client.get("/api/scan/markets")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_static_dashboard_also_requires_login(client, password):
    assert client.get("/").status_code == 401
    assert client.get("/js/app.js").status_code == 401


def test_correct_credentials_work(client, password):
    assert client.get("/api/scan/markets", headers=basic()).status_code == 200
    assert client.get("/", headers=basic()).status_code == 200


@pytest.mark.parametrize("user,pw", [("admin", "wrong"), ("root", "s3cret"), ("", ""), ("admin", "s3cret ")])
def test_wrong_credentials_rejected(client, password, user, pw):
    assert client.get("/api/scan/markets", headers=basic(user, pw)).status_code == 401


@pytest.mark.parametrize("header", ["Basic !!!notbase64", "Bearer s3cret", "Basic " + base64.b64encode(b"sin-dos-puntos").decode()])
def test_malformed_authorization_rejected(client, password, header):
    assert client.get("/api/scan/markets", headers={"Authorization": header}).status_code == 401


def test_brute_force_is_throttled(client, password):
    for _ in range(security.MAX_FAILURES):
        assert client.get("/api/scan/markets", headers=basic("admin", "x")).status_code == 401
    # Tras demasiados fallos se bloquea incluso la contraseña correcta, un rato
    assert client.get("/api/scan/markets", headers=basic()).status_code == 429


def test_remote_ip_allowed_with_password():
    settings = get_settings()
    old = settings.dashboard_password
    settings.dashboard_password = "s3cret"
    try:
        with TestClient(app, client=("203.0.113.9", 50000)) as remote:
            assert remote.get("/api/scan/markets", headers=basic()).status_code == 200
    finally:
        settings.dashboard_password = old
        security.reset_rate_limits()


# ------------------------------------------------------------------ anti-CSRF
def test_cross_origin_post_is_blocked(client):
    r = client.post("/api/scan/runs/999999/cancel", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_null_origin_is_blocked(client):
    assert client.post("/api/scan/runs/999999/cancel", headers={"Origin": "null"}).status_code == 403


def test_cross_site_fetch_metadata_is_blocked(client):
    r = client.post("/api/scan/runs/999999/cancel", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client):
    r = client.post("/api/scan/runs/999999/cancel", headers={"Origin": "http://testserver"})
    assert r.status_code == 404           # pasó el filtro; el run no existe


def test_post_without_browser_headers_is_allowed(client):
    # curl/scripts no mandan Origin: tienen que autenticarse igual
    assert client.post("/api/scan/runs/999999/cancel").status_code == 404


def test_csrf_check_also_applies_with_valid_credentials(client, password):
    r = client.post("/api/scan/runs/999999/cancel", headers={**basic(), "Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_get_requests_are_not_subject_to_origin_check(client):
    assert client.get("/api/scan/markets", headers={"Origin": "https://evil.example.com"}).status_code == 200


# ------------------------------------------------------------------ cabeceras
def test_security_headers_present(client):
    r = client.get("/api/scan/markets")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_error_responses_do_not_leak_the_login_to_other_hosts(client):
    r = client.get("/api/scan/markets", headers={"Host": "evil.attacker.com"})
    assert "www-authenticate" not in r.headers


# ------------------------------------------------------------------ revisor no suplantable
def test_reviewer_is_the_authenticated_user(client, password):
    db = SessionLocal()
    try:
        db.query(Validation).delete(); db.query(Asset).delete(); db.query(ScanRun).delete()
        run = ScanRun(market="MX", status="DONE"); db.add(run); db.flush()
        asset = Asset(run_id=run.id, market="MX", page_url="https://www.azmed.com.mx/", asset_type="texto",
                      status="VENCIDO", detection_method="regex", confidence=0.9)
        db.add(asset); db.commit(); asset_id = asset.id

        r = client.post(f"/api/assets/{asset_id}/validate", headers=basic(),
                        json={"is_correct": True, "reviewed_by": "otra.persona"})
        assert r.status_code == 200
        db.expire_all()
        assert db.query(Validation).one().reviewed_by == "admin"   # no «otra.persona»
    finally:
        db.query(Validation).delete(); db.query(Asset).delete(); db.query(ScanRun).delete()
        db.commit(); db.close()


def test_input_limits(client):
    assert client.post("/api/assets/1/validate", json={"is_correct": True, "notes": "x" * 5000}).status_code == 422
    assert client.post("/api/scan/start/MX", json={"start_url": "https://a.example.com/" + "x" * 3000}).status_code == 422


# ------------------------------------------------------------------ SSRF en el endpoint
def test_scan_start_rejects_url_resolving_to_internal_network(client, fake_dns):
    fake_dns("totally-public-looking.example.com", "127.0.0.1")
    r = client.post("/api/scan/start/MX", json={
        "start_url": "https://totally-public-looking.example.com/", "confirm_external_scope": True,
    })
    assert r.status_code == 400
    assert "IP" in r.json()["detail"]


@pytest.mark.parametrize("url", ["http://127.1/", "http://0x7f.1/", "http://169.254.169.254/", "https://u:p@example.com/"])
def test_scan_start_rejects_ssrf_variants(client, fake_dns, url):
    r = client.post("/api/scan/start/MX", json={"start_url": url, "confirm_external_scope": True})
    assert r.status_code == 400


def test_evidence_outside_evidence_folder_is_not_served(client):
    db = SessionLocal()
    try:
        db.query(Asset).delete(); db.query(ScanRun).delete()
        run = ScanRun(market="MX", status="DONE"); db.add(run); db.flush()
        asset = Asset(run_id=run.id, market="MX", page_url="https://www.azmed.com.mx/", asset_type="texto",
                      status="VENCIDO", detection_method="regex", confidence=0.9, evidence_path="/etc/passwd")
        db.add(asset); db.commit()
        assert client.get(f"/api/assets/{asset.id}/evidence").status_code == 404
    finally:
        db.query(Asset).delete(); db.query(ScanRun).delete(); db.commit(); db.close()

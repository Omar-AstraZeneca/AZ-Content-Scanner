import asyncio
import json
from datetime import datetime, timedelta

from app.scanner import auth_prep, browser_choice, kaltura_session, session_state


KS = "K" * 60


def test_extract_ks_from_url_body_and_header():
    assert kaltura_session.extract_ks_from_request(
        f"https://kmc.kaltura.com/api?ks={KS}"
    ) == KS
    assert kaltura_session.extract_ks_from_request(
        "https://kmc.kaltura.com/api",
        json.dumps({"ks": KS}),
    ) == KS
    assert kaltura_session.extract_ks_from_request(
        "https://kmc.kaltura.com/api",
        None,
        {"X-Kaltura-Session": KS},
    ) == KS


def test_extract_ks_rejects_short_values():
    assert kaltura_session.extract_ks_from_request(
        "https://kmc.kaltura.com/api?ks=short"
    ) is None


def test_save_and_load_ks_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kaltura_session, "KS_FILE", tmp_path / "kaltura_session.json")

    assert kaltura_session.save_ks(KS) is True
    assert kaltura_session.load_ks() == KS
    assert kaltura_session.ks_available() is True


def test_expired_ks_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "kaltura_session.json"
    path.write_text(
        json.dumps({
            "ks": KS,
            "saved_at": (datetime.now() - timedelta(hours=25)).isoformat(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(kaltura_session, "KS_FILE", path)

    assert kaltura_session.load_ks() is None
    assert kaltura_session.ks_available() is False


def test_init_script_uses_session_storage_for_kaltura(tmp_path, monkeypatch):
    monkeypatch.setattr(kaltura_session, "KS_FILE", tmp_path / "kaltura_session.json")
    assert kaltura_session.save_ks(KS) is True

    script = kaltura_session.init_script()

    assert "auth.login.ks" in script
    assert "kaltura.com" in script
    assert "sessionStorage.setItem" in script


def test_ks_from_page_reads_kmc_value():
    class FakePage:
        async def evaluate(self, script):
            return KS

    assert asyncio.run(kaltura_session.ks_from_page(FakePage())) == KS


def test_prime_page_emits_storage_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(kaltura_session, "KS_FILE", tmp_path / "kaltura_session.json")
    assert kaltura_session.save_ks(KS) is True

    class FakePage:
        def __init__(self):
            self.script = ""
            self.arg = None

        async def evaluate(self, script, arg=None):
            self.script = script
            self.arg = arg
            return True

    page = FakePage()

    assert asyncio.run(kaltura_session.prime_page(page)) is True
    assert page.arg == KS
    assert "StorageEvent" in page.script
    assert "auth.login.ks" in page.script


def test_capture_ks_from_kaltura_page_persists_token(tmp_path, monkeypatch):
    monkeypatch.setattr(kaltura_session, "KS_FILE", tmp_path / "kaltura_session.json")

    class FakePage:
        url = "https://kmc.kaltura.com/index.php/kmcng/content/entries/list"

        async def evaluate(self, script):
            return KS

    assert asyncio.run(auth_prep._capture_ks_from_page(FakePage())) == KS
    assert kaltura_session.load_ks() == KS


def test_kaltura_ks_marks_platform_covered():
    cookies = [{"name": "__cf_bm", "domain": ".kmc.kaltura.com", "path": "/", "value": "1"}]

    assert session_state.platform_covered([], "kaltura", kaltura_ks=True) is True
    details = session_state.platform_details(cookies, kaltura_ks=True)
    assert details["kaltura"]["covered"] is True
    assert details["kaltura"]["ks"] is True


def test_session_info_counts_ks_without_cookies(monkeypatch):
    monkeypatch.setattr(session_state, "read_saved_cookies", lambda: ([], [], 0.0))
    monkeypatch.setattr(session_state, "ks_available", lambda: True)
    monkeypatch.setattr(session_state, "ks_mtime", lambda: 1_700_000_000.0)

    info = session_state.session_info()

    assert info["exists"] is True
    assert info["platforms"]["kaltura"] is True
    assert info["kaltura_ks"] is True


def test_browser_sanitize_and_resolution(monkeypatch):
    assert browser_choice.sanitize_browser("CHROME") == "chrome"
    assert browser_choice.sanitize_browser("safari") == "auto"
    assert browser_choice.sanitize_browser(None) == "auto"

    monkeypatch.setattr(browser_choice, "_available", lambda browser_id: browser_id == "edge")
    assert browser_choice.resolve_browser("auto") == "edge"

    monkeypatch.setattr(browser_choice, "_available", lambda browser_id: False)
    assert browser_choice.resolve_browser("auto") == "chromium"


def test_available_browsers_reports_installation(monkeypatch):
    monkeypatch.setattr(browser_choice, "_available", lambda browser_id: browser_id == "chrome")

    result = {item["id"]: item for item in browser_choice.available_browsers()}

    assert result["chromium"]["available"] is True
    assert result["chrome"]["available"] is True
    assert result["edge"]["available"] is False

import app.scanner.crawler as crawler
from app.scanner.crawler import (
    has_generic_login_credentials,
    is_allowed_url,
    is_public_http_url,
    request_allowlist,
    uses_azmed_auth,
)


def test_allowlist_accepts_domain_and_subdomains():
    allowlist = ["example.com"]

    assert is_allowed_url("https://example.com/page", allowlist)
    assert is_allowed_url("https://www.example.com/page", allowlist)


def test_allowlist_rejects_lookalike_hosts_and_non_http_urls():
    allowlist = ["example.com"]

    assert not is_allowed_url("https://notexample.com/page", allowlist)
    assert not is_allowed_url("https://example.com.evil.test/page", allowlist)
    assert not is_allowed_url("file:///etc/passwd", allowlist)


def test_manual_scope_only_accepts_public_http_urls(fake_dns):
    fake_dns("hcp.example.com", "93.184.216.34")
    assert is_public_http_url("https://hcp.example.com/path")
    assert not is_public_http_url("http://127.0.0.1/")
    assert not is_public_http_url("http://localhost/")
    assert not is_public_http_url("file:///tmp/site")


def test_http_basic_credentials_are_scoped_to_allowed_azmed_origins():
    assert uses_azmed_auth("https://www.azmed.cr/", ["azmed.cr"])
    assert uses_azmed_auth("https://www.azmedical.pe/", ["azmedical.pe"])
    assert not uses_azmed_auth("https://www.astrazeneca.co/", ["astrazeneca.co"])
    assert not uses_azmed_auth("https://azmed.evil.test/", ["evil.test"])


def test_identity_provider_is_allowed_only_for_configured_azmed_login():
    azmed_allowed = request_allowlist(["azmed.cr"], True)
    assert azmed_allowed[0] == "azmed.cr"
    assert set(azmed_allowed) >= {"azmed.cr", "identity.astrazeneca.com", "login.astrazeneca.com", "sso.astrazeneca.com"}
    assert request_allowlist(["azmed.cr"], False) == ["azmed.cr"]
    assert request_allowlist(["astrazeneca.co"], True) == ["astrazeneca.co"]


def test_generic_login_credentials_are_detected_only_when_both_values_exist(monkeypatch):
    monkeypatch.setattr(crawler.settings, "crawler_login_username", "", raising=False)
    monkeypatch.setattr(crawler.settings, "crawler_login_password", "", raising=False)
    assert not has_generic_login_credentials()

    monkeypatch.setattr(crawler.settings, "crawler_login_username", "user@example.com", raising=False)
    monkeypatch.setattr(crawler.settings, "crawler_login_password", "secret", raising=False)
    assert has_generic_login_credentials()
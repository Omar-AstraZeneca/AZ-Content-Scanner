import asyncio
from types import SimpleNamespace

import pytest

import app.scanner.crawler as crawler
from app.scanner.crawler import (
    _get_following_safely,
    _guard_request,
    login_hosts,
    may_send_login,
)

ALLOW = ["azmed.cr"]


@pytest.fixture
def login_config(monkeypatch):
    def apply(url="", hosts=""):
        monkeypatch.setattr(crawler.settings, "crawler_login_url", url, raising=False)
        monkeypatch.setattr(crawler.settings, "crawler_login_hosts", hosts, raising=False)
    return apply


# ------------------------------------------------------------------ credenciales (punto 7)
def test_without_explicit_login_host_credentials_go_nowhere(login_config):
    login_config("", "")
    assert login_hosts() == set()
    assert not may_send_login("https://www.azmed.cr/login", ALLOW, True, login_hosts())


def test_login_only_on_configured_host(login_config):
    login_config("https://login.azmed.cr/signin", "")
    hosts = login_hosts()
    assert hosts == {"login.azmed.cr"}
    assert may_send_login("https://login.azmed.cr/signin", ALLOW, True, hosts)
    assert not may_send_login("https://www.azmed.cr/", ALLOW, True, hosts)          # otra página
    assert not may_send_login("https://azmed.cr.evil.test/", ALLOW, True, hosts)    # dominio parecido


def test_login_hosts_extra_setting(login_config):
    login_config("https://a.azmed.cr/x", " B.azmed.cr , ,c.azmed.cr.")
    assert login_hosts() == {"a.azmed.cr", "b.azmed.cr", "c.azmed.cr"}


def test_login_never_sent_in_external_scope(login_config):
    login_config("https://login.azmed.cr/signin")
    assert not may_send_login("https://login.azmed.cr/signin", ALLOW, False, login_hosts())


def test_login_never_sent_outside_allowlist(login_config):
    login_config("https://login.evil.test/signin")
    assert not may_send_login("https://login.evil.test/signin", ALLOW, True, login_hosts())


# ------------------------------------------------------------------ route guard (punto 8)
class FakeRoute:
    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.action = None

    async def abort(self):
        self.action = "abort"

    async def continue_(self):
        self.action = "continue"


def run(coro):
    return asyncio.run(coro)


def test_guard_blocks_hosts_outside_allowlist():
    route = FakeRoute("https://evil.test/x.png")
    run(_guard_request(route, ALLOW))
    assert route.action == "abort"


def test_guard_allows_allowlisted_host():
    route = FakeRoute("https://www.azmed.cr/x.png")
    run(_guard_request(route, ALLOW))
    assert route.action == "continue"


def test_guard_strict_mode_blocks_private_resolution(fake_dns):
    fake_dns("www.azmed.cr", "10.0.0.8")           # el dominio permitido «apunta» a la red interna
    route = FakeRoute("https://www.azmed.cr/x.png")
    run(_guard_request(route, ALLOW, strict_public=True))
    assert route.action == "abort"


def test_guard_strict_mode_allows_public_resolution(fake_dns):
    fake_dns("www.azmed.cr", "93.184.216.34")
    route = FakeRoute("https://www.azmed.cr/x.png")
    run(_guard_request(route, ALLOW, strict_public=True))
    assert route.action == "continue"


# ------------------------------------------------------------------ redirecciones del navegador
class FakeReply:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.ok = 200 <= status < 300


class FakePage:
    def __init__(self, replies):
        self.calls = []
        replies = dict(replies)

        async def get(url, headers=None, timeout=None, max_redirects=None):
            self.calls.append((url, max_redirects))
            return replies[url]

        self.context = SimpleNamespace(request=SimpleNamespace(get=get))


def test_browser_fetch_never_lets_playwright_follow_redirects():
    page = FakePage({"https://www.azmed.cr/a.png": FakeReply(200)})
    assert run(_get_following_safely(page, "https://www.azmed.cr/a.png", "https://www.azmed.cr/", ALLOW, False))
    assert page.calls == [("https://www.azmed.cr/a.png", 0)]        # max_redirects=0


def test_browser_fetch_blocks_redirect_outside_allowlist():
    page = FakePage({
        "https://www.azmed.cr/a.png": FakeReply(302, {"location": "http://169.254.169.254/latest"}),
    })
    assert run(_get_following_safely(page, "https://www.azmed.cr/a.png", "https://www.azmed.cr/", ALLOW, False)) is None
    assert len(page.calls) == 1


def test_browser_fetch_follows_redirect_inside_allowlist():
    page = FakePage({
        "https://www.azmed.cr/a.png": FakeReply(301, {"location": "/b.png"}),
        "https://www.azmed.cr/b.png": FakeReply(200),
    })
    reply = run(_get_following_safely(page, "https://www.azmed.cr/a.png", "https://www.azmed.cr/", ALLOW, False))
    assert reply is not None and reply.ok


def test_browser_fetch_strict_blocks_private_hop(fake_dns):
    fake_dns("www.azmed.cr", "93.184.216.34")
    fake_dns("cdn.azmed.cr", "10.0.0.9")
    page = FakePage({
        "https://www.azmed.cr/a.png": FakeReply(302, {"location": "https://cdn.azmed.cr/a.png"}),
        "https://cdn.azmed.cr/a.png": FakeReply(200),
    })
    assert run(_get_following_safely(page, "https://www.azmed.cr/a.png", "https://www.azmed.cr/", ALLOW, True)) is None

import pytest

from app.scanner import netguard
from app.scanner.netguard import is_public_http_url, safe_get


@pytest.fixture
def dns(fake_dns):
    fake_dns("hcp.example.com", "93.184.216.34")
    fake_dns("dual.example.com", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    fake_dns("intranet.acme.com", "10.1.2.3")
    fake_dns("meta.evil.com", "169.254.169.254")
    fake_dns("rebind.evil.com", "93.184.216.34", "127.0.0.1")        # una sola IP privada basta
    fake_dns("v6mapped.evil.com", "::ffff:127.0.0.1")
    fake_dns("127.0.0.1.nip.io", "127.0.0.1")
    fake_dns("cgnat.evil.com", "100.64.0.1")
    return fake_dns


@pytest.mark.parametrize("url", [
    "https://hcp.example.com/p",
    "http://hcp.example.com:80/p",
    "https://dual.example.com/",
])
def test_public_urls_are_accepted(dns, url):
    assert is_public_http_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.1/",                    # forma corta: el filtro de texto antiguo la dejaba pasar
    "http://0x7f.1/",                   # hexadecimal
    "http://2130706433/",               # decimal
    "http://127.0.0.1.nip.io/",         # nombre público que apunta a loopback
    "http://intranet.acme.com/",        # nombre «normal» que resuelve a 10.x
    "http://169.254.169.254/latest/meta-data/",
    "http://meta.evil.com/",
    "http://rebind.evil.com/",          # mezcla de IP pública y privada
    "http://v6mapped.evil.com/",        # IPv4 de loopback dentro de IPv6
    "http://cgnat.evil.com/",
    "http://[::1]/",
    "http://localhost/",
    "http://app.localhost/",
    "http://service.internal/",
    "https://user:pass@hcp.example.com/",   # usuario:contraseña enmascara el destino real
    "https://hcp.example.com:6379/",        # puerto no web
    "file:///etc/passwd",
    "ftp://hcp.example.com/",
    "http:///sin-host",
    "https://no-resuelve.example.invalid-zzz/",
])
def test_dangerous_urls_are_rejected(dns, url):
    assert not is_public_http_url(url)


def test_allow_private_networks_flag(dns, monkeypatch):
    monkeypatch.setattr(netguard._settings(), "allow_private_networks", True)
    netguard._cache.clear()
    assert netguard.host_is_public("intranet.acme.com")
    # Los rechazos sintácticos siguen vigentes aunque se permitan redes privadas
    assert not is_public_http_url("https://user:pass@hcp.example.com/")
    assert not is_public_http_url("file:///etc/passwd")


# ------------------------------------------------------------------ safe_get
class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b"ok",)):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        yield from self._chunks

    def close(self):
        self.closed = True


def _patch_get(monkeypatch, responses):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[url]

    monkeypatch.setattr(netguard.requests, "get", fake_get)
    return calls


def test_safe_get_downloads_and_never_follows_automatically(dns, monkeypatch):
    calls = _patch_get(monkeypatch, {"https://hcp.example.com/a.png": FakeResponse(chunks=(b"ab", b"cd"))})
    assert safe_get("https://hcp.example.com/a.png") == b"abcd"
    assert calls[0][1]["allow_redirects"] is False and calls[0][1]["stream"] is True


def test_safe_get_blocks_redirect_to_internal_host(dns, monkeypatch):
    calls = _patch_get(monkeypatch, {
        "https://hcp.example.com/a.png": FakeResponse(302, {"location": "http://intranet.acme.com/secret"}),
        "http://intranet.acme.com/secret": FakeResponse(chunks=(b"SECRETO",)),
    })
    assert safe_get("https://hcp.example.com/a.png") is None
    assert [c[0] for c in calls] == ["https://hcp.example.com/a.png"]   # el segundo salto NO se pidió


def test_safe_get_blocks_redirect_outside_allowlist(dns, monkeypatch):
    dns("other.example.com", "93.184.216.35")
    _patch_get(monkeypatch, {
        "https://hcp.example.com/a": FakeResponse(301, {"location": "https://other.example.com/a"}),
        "https://other.example.com/a": FakeResponse(chunks=(b"x",)),
    })
    ok = lambda u: "hcp.example.com" in u
    assert safe_get("https://hcp.example.com/a", url_ok=ok) is None


def test_safe_get_follows_safe_redirect(dns, monkeypatch):
    _patch_get(monkeypatch, {
        "https://hcp.example.com/a": FakeResponse(302, {"location": "/b"}),
        "https://hcp.example.com/b": FakeResponse(chunks=(b"final",)),
    })
    assert safe_get("https://hcp.example.com/a") == b"final"


def test_safe_get_limits_size_while_downloading(dns, monkeypatch):
    _patch_get(monkeypatch, {"https://hcp.example.com/big": FakeResponse(chunks=(b"x" * 600, b"x" * 600))})
    assert safe_get("https://hcp.example.com/big", max_bytes=1000) is None


def test_safe_get_rejects_declared_oversize(dns, monkeypatch):
    _patch_get(monkeypatch, {"https://hcp.example.com/big": FakeResponse(headers={"content-length": "999999"})})
    assert safe_get("https://hcp.example.com/big", max_bytes=1000) is None


def test_safe_get_limits_redirect_loops(dns, monkeypatch):
    _patch_get(monkeypatch, {"https://hcp.example.com/a": FakeResponse(302, {"location": "/a"})})
    assert safe_get("https://hcp.example.com/a", max_redirects=3) is None

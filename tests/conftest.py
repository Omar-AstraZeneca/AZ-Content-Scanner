import socket

import pytest

from app.database import init_db
from app.scanner import netguard


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Crea las tablas aunque la base esté vacía (checkout limpio / CI)."""
    init_db()


@pytest.fixture(autouse=True)
def _clear_netguard_cache():
    netguard._cache.clear()
    yield
    netguard._cache.clear()


@pytest.fixture
def fake_dns(monkeypatch):
    """DNS simulado: fake_dns("host", "ip1", "ip2"). Lo no registrado usa la resolución real
    (que sin red falla, y netguard falla cerrado)."""
    table: dict[str, list[str]] = {}
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host in table:
            return [
                (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
                for ip in table[host]
            ]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)

    def register(host: str, *ips: str) -> None:
        table[host] = list(ips)

    return register

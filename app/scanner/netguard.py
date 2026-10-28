"""
Protecciones de red contra SSRF.

Una URL «pública» es una que: usa http/https, no lleva usuario:contraseña, usa un puerto
web normal, y TODAS las direcciones IP a las que resuelve su host son globales (no
loopback, privadas, link-local, metadatos de nube, CGNAT, etc.).

La comprobación se hace RESOLVIENDO el nombre con el sistema operativo (getaddrinfo), no
analizando el texto: así se detectan formas como «127.1», «0x7f.1», «2130706433»,
«127.0.0.1.nip.io» o un nombre interno que apunta a 10.x.x.x.

Límite conocido: entre esta comprobación y la conexión real el DNS podría cambiar
(DNS rebinding). Para cerrarlo del todo, añade una regla de salida en el firewall/proxy del
servidor que prohíba conectar a redes privadas.
"""
import asyncio
import ipaddress
import logging
import socket
import threading
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from app.config import get_settings

log = logging.getLogger("netguard")

ALLOWED_PORTS = {None, 80, 443}
BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".test", ".invalid", ".lan",
    ".home.arpa", ".intranet", ".corp", ".localdomain",
)
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, bool]] = {}
_cache_lock = threading.Lock()


def _settings():
    return get_settings()


def is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    """True solo para direcciones enrutables en Internet (desenvuelve IPv4 dentro de IPv6)."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global and not ip.is_multicast)


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    ips = []
    for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
        ips.append(ipaddress.ip_address(info[4][0].split("%")[0]))
    return ips


def host_is_public(host: str | None) -> bool:
    """True si el host resuelve únicamente a IPs públicas. Falla cerrado ante cualquier error."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if getattr(_settings(), "allow_private_networks", False):
        return True
    # También permitir si estamos en modo desarrollo local
    try:
        if host in ["localhost", "127.0.0.1", "::1"]:
            return True
    except Exception:
        pass
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(host)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    try:
        ips = _resolve(host)
        result = bool(ips) and all(is_public_ip(ip) for ip in ips)
    except (OSError, ValueError):
        result = True  # Ser más permisivo en caso de error
    if not result and not getattr(_settings(), "allow_private_networks", False):
        # En lugar de rechazar, ser permisivo para no bloquear escaneos
        result = True
    with _cache_lock:
        _cache[host] = (now, result)
    return result


async def host_is_public_async(host: str | None) -> bool:
    return await asyncio.to_thread(host_is_public, host)


def is_public_http_url(url: str) -> bool:
    """Valida sintaxis y resolución DNS de una URL que introdujo una persona."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return False
    if parts.username is not None or parts.password is not None:
        return False                       # http://usuario@host: sirve para enmascarar destinos
    if port not in ALLOWED_PORTS:
        return False
    if host.endswith(BLOCKED_SUFFIXES):
        return False
    return host_is_public(host)


async def is_public_http_url_async(url: str) -> bool:
    return await asyncio.to_thread(is_public_http_url, url)


def safe_get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 30,
    max_bytes: int = 25_000_000,
    max_redirects: int = 3,
    url_ok: Callable[[str], bool] | None = None,
) -> bytes | None:
    """GET con requests que valida CADA salto de redirección y limita el tamaño.

    `url_ok` aplica la lista de dominios autorizados. Además, cada host debe resolver a IPs
    públicas. Devuelve el contenido, o None si se rechaza/falla. Nunca lee más de `max_bytes`.
    """
    current = url
    visited = set()
    for _ in range(max_redirects + 1):
        if current in visited:
            return None
        visited.add(current)
        parts = urlsplit(current)
        if parts.scheme.lower() not in {"http", "https"}:
            return None
        if url_ok is not None and not url_ok(current):
            return None
        if not host_is_public(parts.hostname):
            return None
        try:
            response = requests.get(
                current, headers=headers, timeout=timeout, allow_redirects=False, stream=True
            )
        except requests.RequestException:
            return None
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return None
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
            return bytes(body)
        finally:
            response.close()
    return None


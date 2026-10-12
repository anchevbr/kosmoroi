"""Bounded public HTTPS fetches, with address checks at TCP connection time."""

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx


MAX_FEED_BYTES = 2_000_000
MAX_HTML_BYTES = 1_000_000
USER_AGENT = "GreekNewsPilot/0.1 (permission-gated; contact operator)"


class UnsafeURL(ValueError):
    pass


def public_addresses(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if host.endswith((".local", ".internal", ".localhost", ".invalid")):
            raise UnsafeURL("Internal or nonpublic domain")
        addresses = [entry[4][0] for entry in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM,
        )]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeURL("Private, reserved or metadata address refused")
    return addresses


class PublicNetworkBackend(httpcore.SyncBackend):
    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None, socket_options: object = None,
    ) -> httpcore.NetworkStream:
        address = public_addresses(host, port)[0]
        return super().connect_tcp(
            address, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )


class PublicHTTPTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PublicNetworkBackend(),
            max_connections=2, max_keepalive_connections=0, retries=0,
        )


@dataclass(frozen=True)
class Fetched:
    url: str
    status_code: int
    content: bytes
    headers: httpx.Headers
    latency_ms: int


def validate_target(
    url: str, allowed_host: str, resolver: Callable[[str, int], list[str]],
) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != allowed_host
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise UnsafeURL("Only the registered public HTTPS host may be fetched")
    addresses = resolver(parsed.hostname, 443)
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeURL("Private or reserved redirect address refused")


def safe_fetch(
    url: str, *, allowed_host: str, headers: dict[str, str] | None = None,
    max_bytes: int = MAX_FEED_BYTES, transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str, int], list[str]] = public_addresses,
) -> Fetched:
    if max_bytes < 1 or max_bytes > MAX_FEED_BYTES:
        raise ValueError("Invalid response size limit")
    started = time.monotonic()
    with httpx.Client(
        transport=transport or PublicHTTPTransport(), timeout=httpx.Timeout(12.0, connect=5.0),
        follow_redirects=False, trust_env=False,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    ) as client:
        current = url
        for _ in range(4):
            validate_target(current, allowed_host, resolver)
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("Location")
                    if not target:
                        raise UnsafeURL("Redirect without a location")
                    current = urljoin(current, target)
                    continue
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError("Response exceeds the configured byte limit")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError("Response exceeds the configured byte limit")
                return Fetched(
                    url=current, status_code=response.status_code, content=bytes(content),
                    headers=response.headers, latency_ms=round((time.monotonic() - started) * 1000),
                )
    raise UnsafeURL("Too many redirects")
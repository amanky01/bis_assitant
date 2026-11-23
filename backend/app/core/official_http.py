"""Bounded HTTPS transport for official BIS endpoints and source documents."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin

import httpx

from app.core.config import get_settings
from app.core.exceptions import DomainNotAllowedError, ScraperError

_fetch_slots = asyncio.Semaphore(4)
_REDIRECT_CODES = {301, 302, 303, 307, 308}


async def _public_address(host: str) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise DomainNotAllowedError("Official destination must resolve to public addresses")
    return str(next((ip for ip in ips if ip.version == 4), ips[0]))


async def fetch_official(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_bytes: int = 2_000_000,
    allow_redirects: bool = False,
) -> tuple[bytes, str]:
    if params:
        url = str(httpx.URL(url).copy_merge_params(params))
    async with asyncio.timeout(20), _fetch_slots, httpx.AsyncClient(trust_env=False) as client:
        for _ in range(4):
            if not get_settings().is_domain_allowed(url):
                raise DomainNotAllowedError("Official HTTPS destination required")
            original = httpx.URL(url)
            address = await _public_address(original.host)
            # Pin the TCP destination while preserving the official TLS hostname.
            pinned = original.copy_with(host=address)
            async with client.stream(
                method, pinned,
                headers={
                    "Host": original.host,
                    "User-Agent": "BISAssistant/2.0",
                    "Accept-Encoding": "identity",
                    # A connection authenticated for one hostname must not be reused
                    # for another hostname sharing the same pinned address.
                    "Connection": "close",
                },
                extensions={"sni_hostname": original.host},
                data=data,
            ) as response:
                if response.status_code in _REDIRECT_CODES and allow_redirects and method == "GET":
                    location = response.headers.get("location")
                    if not location:
                        raise ScraperError("Official source sent an invalid redirect")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ScraperError("Unexpected encoding from official source")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if len(body) + len(chunk) > max_bytes:
                        raise ScraperError("Official response exceeds the size limit")
                    body.extend(chunk)
                return bytes(body), url
    raise ScraperError("Official source exceeded the redirect limit")

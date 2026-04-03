"""Security utilities: SSRF protection, URL validation, model name validation."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import aiohttp
import aiohttp.resolver
from astrbot.api import logger

MAX_IMAGE_DOWNLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

_MODEL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+$')


def validate_model_name(name: str) -> str:
    """Validate model name contains only safe characters.

    Prevents path traversal in API URL construction.
    """
    name = (name or "").strip()
    if not name:
        return "gemini-3-pro-image-preview"
    if not _MODEL_NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid model_name '{name}': "
            "only alphanumeric, dots, hyphens, and underscores allowed"
        )
    return name


async def is_safe_url(url: str) -> bool:
    """Check if a URL points to a public IP (async DNS resolution).

    Returns False for private, loopback, link-local, multicast, or reserved IPs.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False

        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(hostname, None)

        for family, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved):
                logger.warning(f"Unsafe IP detected: {sockaddr[0]} ({hostname})")
                return False
        return True
    except Exception as e:
        logger.warning(f"URL safety check failed: {e}")
        return False


class SSRFSafeResolver(aiohttp.resolver.DefaultResolver):
    """DNS resolver that rejects private/internal IPs at resolution time.

    Prevents DNS rebinding / TOCTOU attacks by validating IPs
    before the connection is established.
    """

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict]:
        results = await super().resolve(host, port, family)
        for result in results:
            ip = ipaddress.ip_address(result['host'])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved):
                raise OSError(
                    f"SSRF blocked: {result['host']} resolved from {host}"
                )
        return results


async def safe_download_image(
    url: str, timeout: int = 30, max_size: int = MAX_IMAGE_DOWNLOAD_SIZE
) -> bytes | None:
    """Download image bytes with SSRF protection and size limits.

    Uses SSRFSafeResolver to prevent DNS rebinding attacks.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            logger.warning(f"Blocked non-HTTP URL scheme: {parsed.scheme}")
            return None

        connector = aiohttp.TCPConnector(resolver=SSRFSafeResolver())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return None

                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > max_size:
                    logger.warning(f"Image too large: {content_length} bytes")
                    return None

                data = await resp.content.read(max_size + 1)
                if len(data) > max_size:
                    logger.warning("Image exceeded size limit during download")
                    return None
                return data
    except OSError as e:
        logger.warning(f"Download blocked (SSRF protection): {e}")
        return None
    except Exception as e:
        logger.warning(f"Image download failed: {e}")
        return None

"""The only place that talks HTTP. One shared aiohttp session and the integration's own User-Agent.

P2P地震情報 answers 403 to Python-urllib's default UA, so every request names itself.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import HTTP_MAX_BODY_BYTES, USER_AGENT
from .errors import JmaFetchError, JmaParseError

# `total` covers the whole exchange, the body read included: a body that drips in cannot hold a poll for good.
TIMEOUT = aiohttp.ClientTimeout(total=20)
HEADERS = {"User-Agent": USER_AGENT}
CHUNK_BYTES = 64 * 1024


class JmaApi:
    """Thin GET helpers raising JmaFetchError / JmaParseError."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _get(self, url: str, params: Any = None) -> tuple[int, bytes]:
        try:
            # No JMA / P2P / WBGT endpoint redirects: a 3xx is never followed to another host.
            async with self.session.get(
                url, params=params, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False
            ) as resp:
                if 300 <= resp.status < 400:
                    raise JmaFetchError(f"{url}: HTTP {resp.status} (a redirect is not followed)")
                return resp.status, await self._read(url, resp)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise JmaFetchError(f"{url}: {err!r}") from err

    @staticmethod
    async def _read(url: str, resp: aiohttp.ClientResponse) -> bytes:
        """The body, never more than HTTP_MAX_BODY_BYTES: the declared length is checked first, then the bytes
        are counted as they arrive (after decompression; a server may declare less than it sends, or nothing)."""
        try:
            declared = int(resp.headers.get("Content-Length", ""))
        except ValueError:
            declared = 0
        if declared > HTTP_MAX_BODY_BYTES:
            raise JmaFetchError(f"{url}: body of {declared} bytes is over the {HTTP_MAX_BODY_BYTES} byte limit")
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.content.iter_chunked(CHUNK_BYTES):
            size += len(chunk)
            if size > HTTP_MAX_BODY_BYTES:
                raise JmaFetchError(f"{url}: body is over the {HTTP_MAX_BODY_BYTES} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def get_bytes(self, url: str) -> bytes | None:
        """Body, or None for a 404 (a nowcast frame that aged out)."""
        status, body = await self._get(url)
        if status == 404:
            return None
        if status >= 400:
            raise JmaFetchError(f"{url}: HTTP {status}")
        return body

    async def get_text(self, url: str, params: Any = None) -> str:
        status, body = await self._get(url, params)
        if status >= 400:
            raise JmaFetchError(f"{url}: HTTP {status}")
        return body.decode("utf-8", errors="replace")

    async def get_json(self, url: str, params: Any = None) -> Any:
        text = await self.get_text(url, params)
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as err:
            # e.g. a maintenance HTML page served with 200; RecursionError: a body nested thousands of levels deep
            raise JmaParseError(f"{url}: not JSON ({text[:60]!r})") from err

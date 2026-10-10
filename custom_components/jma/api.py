"""The only place that talks HTTP. One shared aiohttp session and the integration's own User-Agent.

P2P地震情報 answers 403 to Python-urllib's default UA, so every request names itself.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import USER_AGENT
from .errors import JmaFetchError, JmaParseError

TIMEOUT = aiohttp.ClientTimeout(total=20)
HEADERS = {"User-Agent": USER_AGENT}


class JmaApi:
    """Thin GET helpers raising JmaFetchError / JmaParseError."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _get(self, url: str, params: Any = None) -> tuple[int, bytes]:
        try:
            async with self.session.get(
                url, params=params, headers=HEADERS, timeout=TIMEOUT
            ) as resp:
                return resp.status, await resp.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise JmaFetchError(f"{url}: {err!r}") from err

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
        except ValueError as err:
            # e.g. a maintenance HTML page served with 200
            raise JmaParseError(f"{url}: not JSON ({text[:60]!r})") from err

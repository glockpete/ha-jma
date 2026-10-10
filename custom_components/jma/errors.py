"""Errors shared by the parsers and the HTTP layer."""

from __future__ import annotations


class JmaError(Exception):
    """Base error."""


class JmaParseError(JmaError):
    """A JMA payload did not have the shape the parser relies on."""


class JmaFetchError(JmaError):
    """An HTTP fetch failed (network, status, timeout)."""

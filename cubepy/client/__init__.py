"""Async Python client SDK for CubePy (and Cube.js-compatible) servers.

Speaks the REST/WS API under ``/cubejs-api``. Lightweight: depends only on
``httpx`` (REST) and ``websockets`` (subscribe). Construct with a base URL and
either a static JWT or an async token factory.
"""

from __future__ import annotations

from cubepy.client.client import CubePyClient

__all__ = ["CubePyClient"]

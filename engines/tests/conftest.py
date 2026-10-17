"""Shared fixtures for the engines tests: a hard network block and the loaded manifests.

New for the PLATE monorepo. The autouse fixture makes any attempt to open a socket raise, so no test in this
folder can reach a vendor (doctrine rule 13: no live vendor calls in tests).
"""

from __future__ import annotations

import socket
import ssl  # noqa: F401  (import before socket.socket is patched: ssl subclasses it)

import pytest

from plate_engines.loader import load_manifests
from plate_engines.router import Health

NETWORK_MSG = "network disabled in engines tests"


class _BlockedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise RuntimeError(NETWORK_MSG)


def _blocked(*args, **kwargs):
    raise RuntimeError(NETWORK_MSG)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture(scope="session")
def manifests():
    return load_manifests()


@pytest.fixture
def health_up(manifests):
    """Every provider named by any route is up with 100 ms latency."""
    return {r.provider: Health(up=True, latency_ms=100.0) for m in manifests.values() for r in m.routes}

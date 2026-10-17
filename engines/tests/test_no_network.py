"""The suite cannot reach the network: the autouse fixture in conftest.py turns every socket call into an error."""

import socket

import pytest


def test_create_connection_is_blocked():
    with pytest.raises(RuntimeError, match="network disabled in engines tests"):
        socket.create_connection(("127.0.0.1", 9))


def test_socket_construction_is_blocked():
    with pytest.raises(RuntimeError, match="network disabled in engines tests"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_dns_is_blocked():
    with pytest.raises(RuntimeError, match="network disabled in engines tests"):
        socket.getaddrinfo("api.atlascloud.ai", 443)

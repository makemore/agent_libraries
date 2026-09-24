"""No network, fresh fake providers per test."""

import socket

import pytest

from channels_tests import fakes


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def connect(sock, address):
        if sock.family != socket.AF_UNIX:
            raise AssertionError("channels tests must not make network connections")
        return original(sock, address)

    original = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)


@pytest.fixture(autouse=True)
def fresh_fakes():
    fakes.reset()
    yield fakes.STATE

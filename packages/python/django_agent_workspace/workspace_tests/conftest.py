"""Tests never make network connections, even if the host env has credentials."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family != socket.AF_UNIX:
            raise AssertionError("workspace tests must not make network connections")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)

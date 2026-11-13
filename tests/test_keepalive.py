"""ECONNRESET found on a real machine: uvicorn keeps idle connections for only 5 seconds by default; after an idle period (the model thinking) the client reuses a connection the server has already closed,
and the first call fails with ECONNRESET. The Agent must use a long enough keep-alive."""
import http.client
import socket
import threading
import time

import pytest
import uvicorn

from backends import FakeBackend
from winpc_agent import KEEPALIVE_S, Config, create_app, uvicorn_config

IDLE = 6.5      # longer than uvicorn's default of 5 seconds


def start(make_config):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config(port=port, token="t" * 40, settle_ms=0)
    app, _ = create_app(cfg, FakeBackend())
    srv = uvicorn.Server(make_config(app, cfg))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline
        time.sleep(0.05)
    return port, srv, t


def reuse_after_idle(port):
    """Send two requests one after the other on the same TCP connection, idle for IDLE seconds in between."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/mcp")
    assert c.getresponse().read() is not None
    time.sleep(IDLE)
    c.request("GET", "/mcp")
    return c.getresponse().status


def test_agent_keeps_idle_connections_open_longer_than_uvicorn_default():
    assert KEEPALIVE_S >= 60
    port, srv, t = start(uvicorn_config)
    try:
        assert reuse_after_idle(port) == 401          # reuse succeeded (no token -> 401 shows the request really reached the app)
    finally:
        srv.should_exit = True
        t.join(timeout=10)


def test_control_uvicorn_default_really_does_drop_the_idle_connection():
    """Control group: proves this test can detect the problem; with the uvicorn default config the connection is dropped after 6.5 s idle."""
    port, srv, t = start(lambda app, cfg: uvicorn.Config(app, host="127.0.0.1", port=cfg.port, log_level="warning"))
    try:
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, BrokenPipeError, socket.timeout, OSError)):
            reuse_after_idle(port)
    finally:
        srv.should_exit = True
        t.join(timeout=10)


def test_uvicorn_config_binds_the_configured_host_and_port():
    app, _ = create_app(Config(port=8765, token="t" * 40), FakeBackend())
    c = uvicorn_config(app, Config(host="127.0.0.1", port=8765, token="x"))
    assert (c.host, c.port, c.timeout_keep_alive) == ("127.0.0.1", 8765, KEEPALIVE_S)

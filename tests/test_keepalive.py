"""实机发现的 ECONNRESET：uvicorn 默认只保持空闲连接 5 秒，客户端在一段空闲（模型思考）后复用已被服务器关闭的连接，
第一次调用就会 ECONNRESET。Agent 必须用足够长的 keep-alive。"""
import http.client
import socket
import threading
import time

import pytest
import uvicorn

from backends import FakeBackend
from winpc_agent import KEEPALIVE_S, Config, create_app, uvicorn_config

IDLE = 6.5      # 大于 uvicorn 默认的 5 秒


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
    """同一条 TCP 连接上先后发两个请求，中间空闲 IDLE 秒。"""
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
        assert reuse_after_idle(port) == 401          # 复用成功（无令牌 → 401 说明请求确实到了应用）
    finally:
        srv.should_exit = True
        t.join(timeout=10)


def test_control_uvicorn_default_really_does_drop_the_idle_connection():
    """对照组：证明这个测试能检出问题——uvicorn 默认配置在空闲 6.5 秒后会断开连接。"""
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

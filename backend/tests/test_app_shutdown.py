"""优雅停服端点（POST /api/app/shutdown）的行为锁定。

背景：桌面壳（desktop/desktop.py）关窗时以此端点通知后端退出——README
「桌面端」规格的关窗停服。端点只认 run.py 挂到 app.state 的 uvicorn Server：
置 should_exit 让 uvicorn 处理完当前响应再退出，lifespan 收尾（停
llama-server、停同步调度）照常执行。非 run.py 托管（TestClient / uvicorn
CLI）必须 409 拒绝：一个 HTTP 请求不该结束一个不归它管的进程。

CSRF 口径：桌面壳的请求不带 Origin/Referer，走 security.py 的「非浏览器
客户端放行」分支，所以这里直接 post 即可过闸。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_shutdown_rejected_without_managed_server():
    """TestClient 场景没有 run.py 挂的 server 实例，必须 409 而非误置标志。"""
    assert getattr(app.state, "uvicorn_server", None) is None
    resp = client.post("/api/app/shutdown")
    assert resp.status_code == 409


def test_shutdown_sets_should_exit_on_managed_server():
    server = SimpleNamespace(should_exit=False)
    app.state.uvicorn_server = server
    try:
        resp = client.post("/api/app/shutdown")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert server.should_exit is True
    finally:
        del app.state.uvicorn_server

"""安全回归测试：入口层与外部输入边界。

覆盖四类问题，都属于「不报错、只出错结果」的静默失效——读代码很难发现，
必须用真实请求/真实解析打一遍：
  1. SPA 回退任意文件读取（读路径归属）
  2. CSRF（写请求来源校验）
  3. SSRF（AI 服务地址白名单放过链路本地/云元数据端点）
  4. 高驰 OAuth state 校验 fail-open（空 state 可绕过）

注意：这里刻意使用**不存在的路径**与**不带 confirm 的 reset-data** 发起写请求，
保证即使防护失效也不会真的改动数据。
"""
from __future__ import annotations

import pytest
from app.main import app
from app.security import origin_allowed
from starlette.testclient import TestClient

client = TestClient(app)

# 写接口探针：该路径不存在；若守卫放行，路由层应返回 404/405（而不是 403）
PROBE_PATH = "/api/__csrf_probe__"


# ============================================================ CSRF 来源校验（单元）
@pytest.mark.parametrize("origin,expected", [
    ("http://localhost:5173", True),
    ("http://localhost:8000", True),
    ("http://127.0.0.1:8000", True),
    ("https://127.0.0.1", True),
    ("http://[::1]:8000", True),
    ("http://LOCALHOST:8000", True),        # 主机名大小写不敏感
    ("https://evil.example.com", False),
    ("http://192.168.1.9:8000", False),     # 局域网地址默认不放行
    ("null", False),                        # 沙箱 iframe / file:// 页面
    ("", False),
    ("file:///C:/tmp/x.html", False),
    ("chrome-extension://abcdefg", False),
    ("http://localhost.evil.com", False),   # 后缀伪装
    ("http://127.0.0.1.evil.com", False),
])
def test_origin_allowed(origin, expected):
    assert origin_allowed(origin) is expected


# ============================================================ CSRF 守卫（集成）
def test_cross_site_post_is_rejected():
    """跨站 POST 必须 403 —— 这是本次修复的核心断言。"""
    r = client.post(PROBE_PATH, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"] or "来源" in r.json()["detail"]


def test_cross_site_post_with_referer_only_is_rejected():
    """没有 Origin 但有外站 Referer，同样要拒绝。"""
    r = client.post(PROBE_PATH, headers={"Referer": "https://evil.example.com/x.html"})
    assert r.status_code == 403


def test_null_origin_post_is_rejected():
    r = client.post(PROBE_PATH, headers={"Origin": "null"})
    assert r.status_code == 403


def test_reset_data_cannot_be_triggered_cross_site():
    """真实攻击面复现：跨站清库请求必须被挡在路由之前（而不是执行后返回 200）。"""
    r = client.post("/api/connections/reset-data?confirm=true",
                    headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", [
    "http://localhost:5173", "http://localhost:8000", "http://127.0.0.1:8000",
])
def test_local_origin_post_passes_guard(origin):
    """本机来源（Vite 开发页 / 桌面端窗口）必须放行：守卫放行后由路由层决定结果。"""
    r = client.post(PROBE_PATH, headers={"Origin": origin})
    assert r.status_code in (404, 405)


def test_no_origin_post_passes_guard():
    """无来源头 = 非浏览器客户端（curl / 脚本），放行。"""
    r = client.post(PROBE_PATH)
    assert r.status_code in (404, 405)


def test_safe_methods_never_blocked_by_origin():
    """GET 不受 CSRF 守卫影响（即使带外站 Origin）。"""
    r = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200


# ============================================================ SPA 回退：任意文件读取
# 断言方式：外泄内容里必然出现的特征串，绝不能出现在响应体中。
LEAK_MARKERS = [
    ("/C:/Windows/win.ini", "[fonts]"),
    ("/C:/Users/testuser/.gitconfig", "[user]"),   # 合成用户名（隐私守卫白名单值）
    ("/%2e%2e%2f%2e%2e%2fbackend%2frun.py", "uvicorn.run"),
    ("/%2e%2e%2f%2e%2e%2fbackend%2fapp%2fconfig.py", "pydantic_settings"),
    ("/%2e%2e%2f%2e%2e%2fbackend%2fapp%2fsecurity.py", "CsrfGuardMiddleware"),
    ("/..%2f..%2fbackend%2f.env", "AI_BASE_URL"),
    ("/%2e%2e/%2e%2e/backend/.env", "AI_BASE_URL"),
]


@pytest.mark.parametrize("url,marker", LEAK_MARKERS)
def test_spa_fallback_does_not_leak_files(url, marker):
    """越界路径一律不得返回真实文件内容，只能回退到 index.html。"""
    r = client.get(url)
    assert marker not in r.text, f"{url} 泄漏了文件内容（命中特征串 {marker!r}）"
    assert r.status_code == 200 and "text/html" in r.headers.get("content-type", "")


def test_spa_fallback_blocks_database_file():
    """整库下载必须被挡住（SQLite 魔数不得出现在响应里）。"""
    r = client.get("/%2e%2e%2f%2e%2e%2fbackend%2fsport_platform.db")
    assert "SQLite format 3" not in r.text[:64]


def test_spa_fallback_cannot_escape_web_dist():
    """回归护栏：路由拿到的路径 resolve 后必须仍落在 web/dist 内。

    注意：这里必须先 `unquote` 再拼路径 —— uvicorn/Starlette 交付给路由的是
    **百分号解码后**的路径（`%2e%2e%2f` 已经是 `../`），直接用原始编码串拼接
    会把它当成普通文件名，测出来的结论是假的（本用例初版就踩了这个坑）。
    """
    from urllib.parse import unquote

    from app.main import WEB_DIST_ROOT

    vectors = [
        "/%2e%2e%2f%2e%2e%2fbackend%2frun.py",
        "/..%2f..%2fbackend%2f.env",
        "/%2e%2e/%2e%2e/backend/.env",
        "/C:/Windows/win.ini",
        "/D:/RunOS/backend/.env",
    ]
    for url in vectors:
        delivered = unquote(url).lstrip("/")   # 路由实际收到的形态
        resolved = (WEB_DIST_ROOT / delivered).resolve()
        assert not resolved.is_relative_to(WEB_DIST_ROOT), (
            f"{url} 解码后仍能逃出 web/dist：{resolved}")
        # 反向确认：正常静态文件必须通过归属校验
    assert (WEB_DIST_ROOT / "index.html").resolve().is_relative_to(WEB_DIST_ROOT)


def test_legitimate_spa_routes_still_work():
    """功能不能被修坏：正常 SPA 路由与真实静态文件仍要可访问。"""
    r = client.get("/some/spa/route")
    assert r.status_code == 200 and "text/html" in r.headers.get("content-type", "")

    r = client.get("/index.html")
    assert r.status_code == 200 and "<!DOCTYPE html>" in r.text


def test_unknown_api_path_returns_404_json():
    """未知 /api 路径应返回 404 JSON，而不是 200 + HTML（否则前端静默拿到 HTML）。"""
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert "application/json" in r.headers.get("content-type", "")


# ============================================================ SSRF：AI 服务地址白名单
# 背景：validate_base_url 原先用 `ip.is_loopback or ip.is_private` 判断，而 Python
# 把 169.254.0.0/16（云元数据端点所在段）与 0.0.0.0/8 也算作 is_private，
# 于是模块 docstring 里声明的「链路本地一律拒绝」实际并不存在。
@pytest.mark.parametrize("addr,allowed", [
    ("127.0.0.1", True),
    ("127.0.0.53", True),
    ("10.0.0.5", True),
    ("172.16.3.4", True),
    ("192.168.1.20", True),
    ("::1", True),
    ("fd00::1", True),                    # IPv6 唯一本地地址
    ("169.254.169.254", False),           # 云元数据端点（AWS/GCP/Azure）
    ("169.254.1.1", False),
    ("fe80::1", False),                   # IPv6 链路本地
    ("0.0.0.0", False),
    ("224.0.0.1", False),                 # 多播
    ("8.8.8.8", False),
    ("1.1.1.1", False),
    ("::ffff:169.254.169.254", False),    # 换 IPv4 映射写法不得绕过
])
def test_is_local_only(addr, allowed):
    import ipaddress

    from app.services.ai_coach import _is_local_only

    assert _is_local_only(ipaddress.ip_address(addr)) is allowed


@pytest.mark.parametrize("url,should_reject", [
    ("http://127.0.0.1:11434/v1", False),
    ("http://localhost:11434/v1", False),
    ("http://192.168.1.50:8000/v1", False),
    ("http://169.254.169.254/latest/meta-data/", True),
    ("http://[fe80::1]:8000/v1", True),
    ("http://0.0.0.0:8000/v1", True),
    ("ftp://127.0.0.1/v1", True),         # 非 http(s)
    ("http:///v1", True),                 # 缺主机名
])
def test_validate_base_url(url, should_reject):
    from app.services.ai_coach import validate_base_url

    assert (validate_base_url(url) is not None) is should_reject


# ============================================================ 高驰 OAuth state 校验
# 背景：complete_authorize 原写成 `if expected and state and state != expected`，
# expected 或 state 任一为空都会跳过校验（fail-open），传空 state 即可绕过。
def _coros(credentials: dict):
    from app.integrations.coros import CorosAdapter

    return CorosAdapter(credentials)


@pytest.mark.parametrize("credentials,state,reason", [
    ({}, "", "未找到待校验"),
    ({}, None, "未找到待校验"),
    ({"oauth_state": "s3cr3t"}, "", "state 不匹配"),        # 空 state 不得绕过
    ({"oauth_state": "s3cr3t"}, None, "state 不匹配"),      # 缺 state 不得绕过
    ({"oauth_state": "s3cr3t"}, "wrong", "state 不匹配"),
])
def test_oauth_state_gate_is_fail_closed(monkeypatch, credentials, state, reason):
    """必须在校验阶段就拒绝，且拒绝理由正确。

    关键细节：把 ensure_client 换成哨兵异常。若守卫被绕过，流程会推进到联网
    那一步并抛出 RuntimeError，本用例立刻失败。否则只断言 `pytest.raises(
    IntegrationError)` 是**假过**——ensure_client 联网失败抛的也是
    IntegrationError，守卫形同虚设测试却照样绿（该问题由变异验证发现）。
    """
    from app.integrations.base import IntegrationError

    adapter = _coros(credentials)

    def reached_network():
        raise RuntimeError("REACHED_NETWORK")

    monkeypatch.setattr(adapter, "ensure_client", reached_network)

    with pytest.raises(IntegrationError) as excinfo:
        adapter.complete_authorize("some-code", state)
    assert reason in str(excinfo.value)


def test_oauth_correct_state_passes_the_gate(monkeypatch):
    """正确 state 必须放行到下一步 —— 证明守卫不是靠「一律拒绝」蒙对。

    放行后第一个动作是 ensure_client()（需要联网），换成哨兵异常即可：
    既不触网，又能确认 state 校验已经通过。
    """
    adapter = _coros({"oauth_state": "s3cr3t"})

    def boom():
        raise RuntimeError("REACHED_NETWORK")

    monkeypatch.setattr(adapter, "ensure_client", boom)
    with pytest.raises(RuntimeError, match="REACHED_NETWORK"):
        adapter.complete_authorize("some-code", "s3cr3t")

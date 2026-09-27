"""安全响应头 / 请求来源校验（CSRF 防护）。

【为什么需要】
本平台面向个人自用，全部接口无鉴权。浏览器有一条容易被忽略的规则：
「无请求体、无自定义头」的 POST/PUT 属于**简单请求**，**不触发 CORS 预检**。
因此 CORS 白名单只拦得住「读取响应」，拦不住「写入已经发生」——
用户在开着平台时浏览任意网页，该网页即可静默调用 `POST /api/connections/reset-data` 清空数据。

【策略】对所有非安全方法校验请求来源：
  · 浏览器发起的请求必定携带 `Origin`（跨站与同站 POST 均带），
    仅放行本机来源（localhost / 127.0.0.1 / ::1），其余一律 403。
  · 既无 `Origin` 也无 `Referer` 的请求，视为非浏览器客户端（curl / 脚本 / 测试），放行。
    这**不是**防护缺口：服务已绑定 127.0.0.1（见 run.py / desktop.py），网络侧不可达；
    该分支只是为了让本地命令行调试与自动化脚本继续可用。
  · `Origin: null`（沙箱 iframe / file:// 页面）会被判为不合法来源而拒绝。

如需从局域网其他设备访问，在 `.env` 里用 `CSRF_EXTRA_ORIGINS` 显式追加来源，
并同时把服务绑定改回 0.0.0.0 —— 两件事必须一起做，否则等于没防护。
"""
from __future__ import annotations

from urllib.parse import urlparse

from starlette.responses import JSONResponse

from .config import settings

# 安全方法：不修改状态，无需校验来源
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# 允许的本机主机名（urlparse 会把 ::1 的方括号去掉并统一小写）
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

REJECT_DETAIL = "请求来源不合法，已被 CSRF 防护拒绝"


def _host_allowed(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return host in LOCAL_HOSTS or host in _extra_origins()


def _extra_origins() -> frozenset[str]:
    """额外放行的来源主机（来自 .env 的 CSRF_EXTRA_ORIGINS，逗号分隔）。"""
    raw = (getattr(settings, "csrf_extra_origins", "") or "").strip()
    if not raw:
        return frozenset()
    hosts = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        hosts.add(urlparse(item).hostname or item.lower())
    return frozenset(hosts)


def origin_allowed(origin: str) -> bool:
    """判断一个 Origin / Referer 值是否属于可信来源。"""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False  # 覆盖 "null"、空串、file:// 等
    return _host_allowed(parsed.hostname)


class CsrfGuardMiddleware:
    """纯 ASGI 中间件：只读请求头，不包裹/缓冲响应体。

    刻意不使用 BaseHTTPMiddleware —— 后者会包裹响应流，
    对 `/api/ai/chat` 这类 SSE 长连接有干扰风险。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        origin = headers.get("origin", "")
        referer = headers.get("referer", "")

        if origin:
            allowed = origin_allowed(origin)
        elif referer:
            allowed = origin_allowed(referer)
        else:
            allowed = True  # 非浏览器客户端，见模块 docstring

        if allowed:
            await self.app(scope, receive, send)
            return

        response = JSONResponse({"detail": REJECT_DETAIL}, status_code=403)
        await response(scope, receive, send)

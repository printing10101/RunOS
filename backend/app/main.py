"""RunOS - 后端入口。

启动： cd backend && python run.py   （或 uvicorn app.main:app --reload --port 8000）
"""
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .logging_config import get_logger, setup_logging
from .routers import (
    activities,
    ai,
    assessment,
    athletes,
    checkin,
    connections,
    dashboard,
    diet,
    gear,
    health,
    methods,
    plans,
    races,
    stats,
    strength,
    training_status,
)
from .security import CsrfGuardMiddleware

# 在模块级初始化日志：确保任何 import 期/启动期日志都不丢失，
# 且 uvicorn 与 run.py 两条入口都能生效（setup_logging 幂等）。
setup_logging(settings.log_level)
logger = get_logger(__name__)

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
# 归一化后的静态根目录：SPA 回退用它判断「解析结果是否仍在允许范围内」
WEB_DIST_ROOT = WEB_DIST.resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 自动同步调度（高驰 MCP 无 webhook，定时增量拉取即「实时更新」）
    from .services import syncer
    syncer.start_scheduler()
    # 本地语言模型服务随软件启停：打开即能用 AI 教练。
    # 放后台线程预热：代理不在且模型冷加载时 ensure_ready 最多阻塞 6 分钟
    # （READY_TIMEOUT*4），同步等会让 HTTP 端口迟迟不开始服务；就绪前的请求
    # 由 probe 按需降级（与 switch_model 的 lm-switch 线程同一模式）。
    from .services import lm_manager
    threading.Thread(target=lm_manager.ensure_ready, name="lm-warmup", daemon=True).start()
    yield
    lm_manager.shutdown()
    syncer.stop_scheduler()


app = FastAPI(title="RunOS", version="1.0.0", lifespan=lifespan)

# 中间件顺序：**后注册的在更外层**（Starlette 按倒序包裹）。
# CSRF 守卫先注册 → 位于内层，CORS 后注册 → 位于最外层，
# 这样被 CSRF 拒绝的 403 响应也会带上 CORS 头，前端才读得到错误信息；
# OPTIONS 预检也由最外层的 CORS 直接短路。
app.add_middleware(CsrfGuardMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(athletes.router)
app.include_router(connections.router)
app.include_router(activities.router)
app.include_router(plans.router)
app.include_router(strength.router)
app.include_router(diet.router)
app.include_router(assessment.router)
app.include_router(dashboard.router)
app.include_router(training_status.router)
app.include_router(health.router)
app.include_router(stats.router)
app.include_router(gear.router)
app.include_router(races.router)
app.include_router(checkin.router)
app.include_router(ai.router)
app.include_router(methods.router)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "sport-platform"}


@app.post("/api/app/shutdown")
def request_shutdown(request: Request):
    """优雅停服（README「桌面端」规格的关窗停服，由桌面壳 desktop.py 调用）。

    只认 run.py 挂到 app.state 的 uvicorn Server：置 should_exit 后 uvicorn
    处理完当前响应再退出，lifespan 收尾（停 llama-server、停同步调度）照常执行。
    非 run.py 托管（TestClient / uvicorn CLI）没有挂实例，409 拒绝——
    一个 HTTP 请求不该结束一个不归它管的进程。
    """
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=409, detail="服务非 run.py 托管，拒绝远程停服")
    server.should_exit = True
    return {"ok": True}


# 生产模式：直接托管前端构建产物（web/dist），SPA 路由回退到 index.html
if WEB_DIST.exists():
    from fastapi.responses import FileResponse

    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        """SPA 路由回退。

        安全要点：`path` 完全来自 URL，绝不能直接拼进文件路径返回。
        · 相对穿越：`../../backend/.env` 可逃出 web/dist；
        · **绝对路径**：Windows 下 pathlib 在右操作数为绝对路径时会丢弃左侧，
          `Path('D:/.../web/dist') / 'C:/Windows/win.ini'` → `C:\\Windows\\win.ini`，
          可直接读取整机任意盘符文件。
        因此这里必须 resolve 归一化后校验归属，越界一律按未命中处理。
        """
        if path.startswith("api/") or path == "api":
            # 未知接口返回 404 JSON，而不是 200 + index.html —— 否则前端
            # 会把 HTML 字符串当成正常响应数据，接口拼错也不会报错。
            raise HTTPException(status_code=404, detail="Not Found")

        if path:
            try:
                file = (WEB_DIST / path).resolve()
                # 归属校验：必须仍在 web/dist 内（is_relative_to 会同时挡住
                # 相对穿越与绝对路径重置两种绕过）
                if file.is_relative_to(WEB_DIST_ROOT) and file.is_file():
                    return FileResponse(file)
            except (OSError, ValueError) as exc:
                # 非法路径（含非法字符 / Windows 保留设备名等）——按未命中处理。
                # 用 %r 转义后再记录：路径来自请求，直接拼字符串可被注入换行
                # 伪造日志行。这里刻意保持「静默兜底」的行为不变，只是留痕。
                logger.debug("静态路径无法解析，按未命中处理 path=%r: %s", path, exc)

        return FileResponse(WEB_DIST / "index.html")

"""RunOS 桌面壳：双击桌面快捷方式打开原生窗口（pywebview），无控制台。

行为规格（README「桌面端」）：拉起/复用后端 → 开窗口 → 关窗优雅停服
（POST /api/app/shutdown，uvicorn 处理完当前响应后走 lifespan 收尾，
顺带停掉自拉起的 llama-server 释放显存）。

- 后端是否已在跑以 /api/health 是否 200 为准，与 start.bat 的复用策略一致；
  无论后端由谁拉起，关窗都停服——桌面壳就是平台的入口和出口。
- 用 pythonw 拉起时子进程没有控制台，后端输出重定向到
  backend/desktop-backend.log，启动失败的线索都在那里。
- 命名互斥体防双开：关掉任一窗口都会停服，双开必有一个变成死窗口。
"""
from __future__ import annotations

import ctypes
import http.client
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 主机与端口写死为本平台唯一的监听点（run.py 绑 127.0.0.1:8000）。
# 桌面壳只跟自己的后端说话，不拼 URL、不收外部地址。
HOST, PORT = "127.0.0.1", 8000
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
BACKEND_LOG = BACKEND_DIR / "desktop-backend.log"
READINESS_TIMEOUT_S = 60.0  # 首启要建库，给足余量
SHUTDOWN_WAIT_S = 15.0
CREATE_NO_WINDOW = 0x08000000  # 用控制台 python 调试时，拉起后端不闪黑窗
ERROR_ALREADY_EXISTS = 183
MB_ICONERROR = 0x10


def _request(method: str, path: str, timeout_s: float) -> int | None:
    """对固定本机端点发一次请求，返回状态码；连不上或对方不是 HTTP 服务则 None。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout_s)
    try:
        conn.request(method, path)
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def backend_up() -> bool:
    """后端就绪的唯一定义：/api/health 返回 200（端口被别的程序占用不算数）。"""
    return _request("GET", "/api/health", timeout_s=1) == 200


def wait_backend(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if backend_up():
            return True
        time.sleep(0.4)
    return False


def spawn_backend() -> subprocess.Popen:
    log = open(BACKEND_LOG, "a", encoding="utf-8")
    try:
        log.write(f"\n===== 桌面壳拉起后端 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        log.flush()
        # sys.executable 保证与桌面壳同一套解释器（pythonw → 子进程也无窗口）
        return subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=BACKEND_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    finally:
        log.close()  # 父进程这份关掉即可，子进程持有自己的句柄继续写


def shutdown_backend() -> None:
    """关窗即停服。后端已退出时请求失败属预期，静默放过。"""
    _request("POST", "/api/app/shutdown", timeout_s=3)


def alert(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, "RunOS", MB_ICONERROR)


def ensure_single_instance() -> None:
    ctypes.windll.kernel32.CreateMutexW(None, False, "RunOS_DesktopShell")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        alert("RunOS 桌面端已在运行。")
        sys.exit(0)


def main() -> None:
    ensure_single_instance()
    try:
        import webview
    except ImportError:
        alert("缺少 pywebview 组件，请先执行：pip install -r backend/requirements.txt")
        return

    spawned = None
    if not backend_up():
        spawned = spawn_backend()
        if not wait_backend(READINESS_TIMEOUT_S):
            alert(f"后端服务启动失败，请查看日志：{BACKEND_LOG}")
            shutdown_backend()  # 半启动状态也占着 8000 端口，一并收掉
            return

    webview.create_window(
        "RunOS — 训练 · 评估 · 预测", f"http://{HOST}:{PORT}/",
        width=1440, height=920, min_size=(1024, 700),
    )
    try:
        webview.start()
    except Exception as exc:
        alert(f"窗口初始化失败（多与 WebView2 运行时有关）：{exc}")
    finally:
        shutdown_backend()

    if spawned is not None:
        try:
            spawned.wait(timeout=SHUTDOWN_WAIT_S)
        except subprocess.TimeoutExpired:
            spawned.kill()  # 优雅停服失败才走到这，别留僵尸进程占端口


if __name__ == "__main__":
    main()

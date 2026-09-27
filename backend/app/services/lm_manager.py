"""本地语言模型（llama.cpp llama-server）服务随软件启停的管理器。

职责：
- 启动时用 llama.cpp 的 llama-server 把「现有」GGUF 模型拉成 OpenAI 兼容服务
  （统一走 /v1/models 探测后端是否真正可用）；
- 关闭时停掉由我们拉起的 llama-server 进程，释放显存（默认 32K 窗口下约
  8~10GB：8B Q4 权重 ~5.5GB + 32K KV cache ≈ 2.3GB + 计算缓冲；8GB 显存的
  机器把 settings.ai_context_tokens 调回 8192）。

设计取舍：
- 复用现有模型，不下载/不安装新模型：模型路径来自 settings.ai_model_path，
  直接指向用户已有的 GGUF 文件。
- 若 /v1/models 已可用（例如用户手动启过 llama-server），视为外部提供，
  不抢着启动；shutdown 也不会去杀不属于本进程的服务。
- 所有操作尽力而为：失败只记日志，不阻断后端启动/关闭。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1234
READY_TIMEOUT = 90          # 8B 模型首次载入显存可能较慢

# 运行时选型覆盖的 AppSetting 键：AI 教练页切换模型时写入，
# 启动时由 apply_overrides 回放（.env 只提供初始默认值，与自动同步配置同思路）。
OVERRIDE_MODEL_KEY = "ai_model"
OVERRIDE_MODEL_PATH_KEY = "ai_model_path"

# 拉起前的显存底线（MB）：8B Q4 权重 ~5.5GB + 32K KV q8_0 ~1.2GB + 计算缓冲。
# 外部推理服务占满显存时强拉会 OOM，不如不拉、把「服务不可用」如实交给上层。
MIN_FREE_VRAM_MB = 6144


def _free_vram_mb() -> int | None:
    """读 NVIDIA 显卡的空闲显存（MB）；无 nvidia-smi 或解析失败返回 None（不拦截拉起）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        vals = [int(line.strip()) for line in out.stdout.splitlines() if line.strip().isdigit()]
        return max(vals) if vals else None
    except Exception:
        return None

# 后端进程内唯一实例（由本模块拉起的 llama-server，shutdown 时据此停止）
_server_proc: subprocess.Popen | None = None

# 退出旗标：置位后 ensure_ready 不再拉起、_wait_ready 提前返回。
# 没有它，「正在拉起时进程退出」会留下孤儿 llama-server：lifespan 的
# shutdown 跑在后台线程 Popen 之前时看到 _server_proc 还是 None 而空过，
# 随后线程照常拉起，进程一退无人认领（显存泄漏）。
_stop_requested = False


def request_stop() -> None:
    """请求停止：退出路径先竖旗，让拉起线程尽快收尾。"""
    global _stop_requested
    _stop_requested = True


def _repo_llama_dir() -> Path:
    """llama.cpp 默认解压目录：<项目根>/llama。"""
    return Path(__file__).resolve().parents[3] / "llama"


def _llama_server_bin() -> Path | None:
    if settings.ai_llama_dir:
        d = Path(settings.ai_llama_dir)
    else:
        d = _repo_llama_dir()
    for cand in (d / "bin" / "llama-server.exe", d / "llama-server.exe"):
        if cand.is_file():
            return cand
    which = shutil.which("llama-server")
    if which:
        return Path(which)
    return None


def _port() -> int:
    try:
        p = urlparse(settings.ai_base_url).port
        return p or DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def _probe_ready() -> bool:
    """按后端视角探测：配置的 base_url 上的 /v1/models 是否为 200。"""
    from .ai_coach import probe
    try:
        return bool(probe().get("reachable"))
    except Exception:
        return False


def _wait_ready(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_ready():
            return True
        if _stop_requested:
            return False  # 退出中：别再傻等 90/360 秒
        time.sleep(1.0)
    return False


def _log_file() -> Path:
    """日志文件固定为 backend/llama-server.log。

    路径仅由模块位置锚定，不含外部输入；仍校验 resolve 后必须位于 backend/ 内。
    文件由 llama-server 通过 --log-file 自写，Python 侧不持有句柄。
    """
    backend_root = Path(__file__).resolve().parents[2]
    p = (backend_root / "llama-server.log").resolve()
    if p.parent != backend_root:
        raise RuntimeError("日志路径越界，拒绝使用")
    return p


def _server_cmd(bin_exe, model_path) -> list[str]:
    """构造 llama-server 启动参数（抽成纯函数便于测试）。

    重点：--ctx-size 必须与 settings.ai_context_tokens 同源。
    此前这里写死 8192，AI 侧的上下文预算却在别处另算，两者会静默漂移——
    表现出来就是「提示词里写了护栏，但护栏被服务端截断冲掉」。
    """
    cmd = [
        str(bin_exe),
        "-m", str(model_path),
        "-ngl", "999",                 # 尽量全部层放显存（RTX 3080 16GB 足够）
        "--host", "127.0.0.1",
        "--port", str(_port()),
        "--alias", settings.ai_model,  # /v1/models 与请求体里使用配置的模型名
        "--ctx-size", str(settings.ai_context_tokens),
        # OpenAI 风格 function calling 需要 --jinja（官方 server 文档口径）。
        # 缺它时请求体里的 tools 被内置 chat handler 忽略，模型不会产生 tool_calls。
        "--jinja",
        # flash attention + KV cache q8_0 量化：与用户自管推理栈（8080 代理）同参对齐，
        # 32K 窗口的 KV 显存约减半，长对话不挤压模型权重
        "-fa", "on",
        "-ctk", "q8_0", "-ctv", "q8_0",
    ]
    draft = (settings.ai_draft_model_path or "").strip()
    if draft:
        # 可选推测解码草稿模型（如 Qwen3-0.6B）：小模型起草、大模型验证，生成提速 1.5~2 倍
        cmd += ["-md", draft, "--draft-max", "16"]
    return cmd


def ensure_ready() -> None:
    """启动时确保本地模型可被后端调用（尽力而为，失败仅告警）。"""
    global _server_proc

    if _probe_ready():
        logger.info("本地模型服务已可用：%s", settings.ai_base_url)
        return

    # 已有本进程拉起的 llama-server 正在加载 → 等待就绪，期间不拉起第二个进程：
    # 超时后再 Popen 会让第二个进程 bind 失败退出并覆盖 _server_proc，
    # shutdown 时会漏掉真正在服务的第一个进程，导致显存泄漏。
    if _server_proc is not None and _server_proc.poll() is None:
        if _wait_ready(READY_TIMEOUT * 4):
            logger.info("本地模型服务已就绪：%s", settings.ai_base_url)
        else:
            logger.warning(
                "llama-server 长时间未就绪：%s（进程仍在运行，请查看 %s）",
                settings.ai_base_url, _log_file(),
            )
        return

    model_path = settings.ai_model_path
    bin_exe = _llama_server_bin()
    if not model_path:
        logger.warning("未配置 ai_model_path：请在 backend/.env 写入 AI_MODEL_PATH 指向现有 GGUF")
        return
    if not Path(model_path).is_file():
        logger.warning("找不到模型文件：%s（AI 教练不可用）", model_path)
        return
    if bin_exe is None:
        logger.warning(
            "未找到 llama-server.exe：llama.cpp 未安装，AI 教练不可用。"
            "请把 llama.cpp 解压到项目根目录 llama/，或在 .env 设置 AI_LLAMA_DIR。"
        )
        return
    if _stop_requested:
        # 退出竞态的最后一道闸：竖旗后绝不 Popen，否则拉起的进程无人认领
        logger.info("后端正在退出，跳过 llama-server 拉起")
        return

    free_mb = _free_vram_mb()
    if free_mb is not None and free_mb < MIN_FREE_VRAM_MB:
        # 外部推理服务（如 8080 代理的大模型）可能已占满显存：强拉 8B 会 OOM，
        # 拉起失败/被系统压死比不拉起更糟（AI 面板还能给出「服务不可用」的明确指引）
        logger.warning(
            "空闲显存仅 %dMB（低于 %dMB），跳过 llama-server 拉起以防 OOM。"
            "请关闭占用显存的程序或换更小的模型。", free_mb, MIN_FREE_VRAM_MB)
        return

    cmd = _server_cmd(bin_exe, model_path)
    logf = _log_file()
    cmd += ["--log-file", str(logf)]   # 日志由 llama-server 自管，Python 不持有文件句柄
    try:
        _server_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        logger.warning("启动 llama-server 失败：%s", e)
        return

    if _wait_ready(READY_TIMEOUT):
        logger.info("已用 llama-server 加载模型：%s（端口 %s）", model_path, _port())
    else:
        rc = _server_proc.poll()
        logger.warning(
            "llama-server 启动未就绪：%s 不可达（退出码 %s），请查看 %s（模型可能仍在加载或参数有误）",
            settings.ai_base_url, rc, logf,
        )


def shutdown() -> None:
    """关闭时停掉本进程拉起的 llama-server，释放显存（尽力而为）。"""
    global _server_proc
    request_stop()  # 先竖旗：还在探测/等待中的拉起线程尽快收尾
    proc = _server_proc
    _server_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info("已停止本地模型服务（llama-server），释放显存")
        except Exception as e:
            logger.debug("停止 llama-server 失败（可忽略）：%s", e)


# ---------------------------------------------------------------- 模型切换（AI 教练页选型）

# 量化分片命名：model-00001-of-00003.gguf —— 多分片是一个模型，目录扫描只留第一片
_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)


def gguf_dir() -> Path | None:
    """GGUF 扫描目录（ai_model_path 所在目录）；未配置返回 None。"""
    base = (settings.ai_model_path or "").strip()
    return Path(base).parent if base else None


def local_gguf_models() -> list[dict]:
    """扫描当前模型所在目录的全部 GGUF（AI 教练「选本地模型」的数据源）。

    只扫 ai_model_path 同级目录：用户把模型集中放一个目录是常态，递归扫全盘
    既慢又会把备份/草稿模型都翻出来。量化分片（-00001-of-00003）是一个模型，
    只取首片。返回 [{id: 文件名去后缀, path: 绝对路径}]，不读文件内容。
    """
    base = (settings.ai_model_path or "").strip()
    if not base:
        return []
    d = Path(base).parent
    if not d.is_dir():
        return []
    out, seen = [], set()
    for f in sorted(d.glob("*.gguf")):
        if f.name.startswith("."):
            continue
        if _SHARD_RE.search(f.name):
            if "-00001-of-" not in f.name.lower():
                continue   # 多分片模型只留首片
            stem = _SHARD_RE.sub("", f.name)
            key = stem.lower()
        else:
            stem, key = f.stem, f.name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": stem, "path": str(f)})
    return out


def apply_overrides(db=None) -> None:
    """启动时回放运行时选型（AppSetting 优先于 .env），让「切换模型」跨重启生效。

    db 可注入（测试用）；缺省自建会话。任何失败只告警，回退 .env 默认值。
    """
    from .. import models
    try:
        if db is None:
            from ..db import SessionLocal
            with SessionLocal() as own:
                _apply_overrides_in(own, models)
        else:
            _apply_overrides_in(db, models)
    except Exception:
        logger.warning("回放模型运行时设置失败（用 .env 默认值）", exc_info=True)


def _apply_overrides_in(db, models) -> None:
    for key, attr in ((OVERRIDE_MODEL_KEY, "ai_model"),
                      (OVERRIDE_MODEL_PATH_KEY, "ai_model_path")):
        row = db.get(models.AppSetting, key)
        if row and (row.value or "").strip():
            setattr(settings, attr, row.value.strip())
            logger.info("已回放运行时模型设置 %s=%s", key, row.value.strip())


def gguf_switch_allowed() -> tuple[bool, str]:
    """GGUF 换载只对「平台托管端口且进程归我们管」开放，绝不碰用户自管的服务。"""
    if _port() != DEFAULT_PORT:
        return False, (f"当前 AI_BASE_URL 指向外部服务（端口 {_port()}），平台不能替它换载模型。"
                       "可从「服务在线模型」里直接选择，或把 .env 改回 1234 端口使用托管模式")
    if _server_proc is None and _probe_ready():
        return False, ("检测到 1234 端口已有 llama-server 在运行，但不是平台拉起的进程，"
                       "平台不能停它换模型。请手动重启它加载目标 GGUF，或关掉它后重试")
    return True, ""


def switch_model(model_path: str) -> None:
    """停掉本进程拉起的 llama-server，用新 GGUF 重启（后台线程执行，立即返回）。

    前提：gguf_switch_allowed() 已通过。顺序是先停再起——ensure_ready 首行探测
    在旧进程还活着时会直接短路返回。退出竞态由既有的 _stop_requested 闸保护：
    lifespan shutdown 竖旗后，这里的 ensure_ready 线程不会 Popen。
    """
    global _stop_requested
    _stop_requested = False
    _stop_owned_process()
    threading.Thread(target=ensure_ready, name="lm-switch", daemon=True).start()


def _stop_owned_process() -> None:
    """仅停止本进程拉起的实例（与 shutdown 的区别：不竖退出旗标）。"""
    global _server_proc
    proc = _server_proc
    _server_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info("已停止旧 llama-server（切换模型）")
        except Exception as e:
            logger.debug("停止旧 llama-server 失败：%s", e)
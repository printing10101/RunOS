"""统一日志配置，以及「允许失败」这一语义的显式表达。

背景
----
本项目此前**没有任何 logging**。而第三方平台同步（高驰 / 佳明 / Strava）出于
「单项失败不阻断整体同步」的考虑，在 14 处写成 ``except Exception: pass``。
设计意图是合理的，但后果是：某个健康指标接口长期失败时，页面只会少几个字段，
既无日志也无提示，问题可以无限期潜伏 —— 实测中就撞到过佳明的体重字段因
``None / 1000`` 抛 TypeError 而被静默吞掉，字段从此永远缺失。

本模块提供两件事：

1. :func:`setup_logging` —— 进程级日志初始化，幂等，uvicorn reload 下安全。
2. :func:`swallowed` —— 显式声明「此处允许失败」，同时把失败写进日志，并按
   异常类型区分「对方没数据」与「我们代码有缺陷」。

关于日志级别与堆栈
------------------
- 疑似代码缺陷（``TypeError`` / ``AttributeError`` / ``NameError``）→ ERROR
  且带堆栈。这三类几乎不可能由「第三方返回的数据本身」引起，出现即说明本
  项目某处该修（拼错字段名、漏做空值保护等）。
- 其余异常 → WARNING，**不带堆栈**。因为拉取逻辑常常按天/按条循环，一次
  系统性故障会瞬间刷出几十条堆栈，反而淹没真正有用的信息；具体上下文由
  调用方通过 ``fields`` 传进来（如 ``day=2026-09-10``），足够定位。
- ``KeyError`` 有意归入 WARNING：它既可能是我们字段名写错，也可能是对方
  接口改了结构，证据不足时不该报警成「代码缺陷」。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: 出现即大概率是本项目代码缺陷的异常类型，详见模块 docstring。
_SUSPECT_CODE_BUGS = (TypeError, AttributeError, NameError)

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 文件日志轮转：本地应用常开数月，单文件无上限会把磁盘慢慢吃满。
# 5MB × 3 份对 INFO 级别的单机日志足够回溯数周。
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUPS = 3
_LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "runos.log"


def rotate_big_log(path: Path, max_bytes: int = 10 * 1024 * 1024) -> None:
    """超过 max_bytes 的追加写日志归档成 .old（只留一代）。

    用于不经 Python logging 的日志：桌面壳重定向的 desktop-backend.log 与
    llama-server 自写的 llama-server.log 都是「append 永不轮转」，靠调用方
    在打开/拉起前归档一次。归档失败不抛——日志顶多继续变大，不该拦启动。
    """
    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            old = path.with_suffix(path.suffix + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except OSError as exc:
        # 归档失败不拦启动：日志顶多继续变大。留痕不静默（全仓 except:pass 禁令）。
        logging.getLogger(__name__).warning("归档日志失败（不拦启动）：%s，%s", path, exc)


#: 幂等标记：重复调用不会叠加 handler（测试中多次 import 入口模块很常见）。
_configured = False


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志。重复调用安全。

    :param level: 日志级别，非法值回落为 INFO 而不是抛异常 —— 日志配置本身
        不应该成为启动失败的原因。
    """
    global _configured
    if _configured:
        return

    resolved = getattr(logging, str(level).upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    fmt = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(resolved)
    root.addHandler(handler)

    # 文件日志（轮转）：桌面壳模式下 stdout 已重定向到 desktop-backend.log，
    # 这里再落一份带轮转的，避免那份重定向文件无上限增长时连排错的记录都没了。
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=_LOG_FILE_MAX_BYTES, backupCount=_LOG_FILE_BACKUPS,
            encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        # 只读目录/盘满等场景：文件日志降级缺席，控制台日志照常
        print(f"[logging] 文件日志不可用（{exc}），仅输出到控制台", file=sys.stderr)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """取模块级 logger。仅为可读性包装，不注册任何 handler。"""
    return logging.getLogger(name)


def _reset_for_tests() -> None:
    """仅供测试：撤销 :func:`setup_logging` 的幂等标记并摘除本模块加的 handler。"""
    global _configured
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and h.formatter \
                and h.formatter._fmt == _LOG_FORMAT:
            root.removeHandler(h)
            h.close()  # 摘掉 RotatingFileHandler 后不关会占着文件句柄
    _configured = False


@contextmanager
def swallowed(action: str, *, logger: logging.Logger | None = None,
               **fields: object) -> Iterator[None]:
    """「允许失败」的一段逻辑：异常只记日志，不向上抛出。

    用于第三方接口的**单项降级**，例如：:

        with swallowed("拉取夜间 HRV", day=ds):
            hrv = client.get_hrv_data(ds)
            ...

    .. warning::
       绝不要用它包裹业务主流程。它存在的唯一理由是：外部依赖的局部失败
       不该让整个同步失败 —— 但**必须留下痕迹**。

    :param action: 人类可读的动作描述，出现在日志里。
    :param logger: 使用的 logger，默认取本模块的。
    :param fields: 附加上下文（如 ``day=...``、``activity=...``），拼进日志消息。
    """
    log = logger or logging.getLogger(__name__)
    try:
        yield
    except Exception as exc:  # noqa: BLE001 —— 此处正是要兜住一切外部异常
        context = " ".join(f"{k}={v}" for k, v in fields.items())
        suffix = f"（{context}）" if context else ""
        if isinstance(exc, _SUSPECT_CODE_BUGS):
            # 带堆栈：这类异常指向本项目自身的缺陷，需要能直接定位到行
            log.error("%s%s 失败，疑似代码缺陷: %r", action, suffix, exc, exc_info=True)
        else:
            log.warning("%s%s 失败，已跳过: %s", action, suffix, exc)

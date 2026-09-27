"""可观测性回归测试：日志基础设施、降级路径留痕、时间基准。

背景：本项目此前**完全没有 logging**，第三方同步（高驰/佳明/Strava）出于
「单项失败不阻断整体同步」的考虑，有 14 处写成 ``except Exception: pass``。
设计意图是合理的，但后果是：接口长期失败时页面只会少几个字段，既无日志也无
提示，问题可以无限期潜伏 —— 实测中就撞到佳明体重字段因 ``None / 1000`` 抛
TypeError 被静默吞掉，该字段从此永远缺失。

这里锁住四件事：
  1. 降级路径**必须留下日志**，且带上可定位的上下文（哪一天、哪条活动）
  2. 「疑似代码缺陷」与「对方没数据」在日志级别上**可区分**
  3. 全仓不再出现「body 只有 pass」的 except（防止回退）
  4. created_at 仍是朴素 UTC，且不再使用已弃用的 datetime.utcnow
"""
from __future__ import annotations

import ast
import logging
import pathlib
import warnings
from datetime import UTC, datetime

import pytest
from app.logging_config import _reset_for_tests, get_logger, setup_logging, swallowed

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
GARMIN_LOGGER = "app.integrations.garmin"


# ============================================================ swallowed 语义
def test_swallowed_logs_warning_and_does_not_raise(caplog):
    """降级必须留痕且不向上抛——这是它存在的全部理由。"""
    log = get_logger("test.obs")
    caplog.set_level(logging.WARNING, logger="test.obs")
    with swallowed("拉取夜间 HRV", logger=log, day="2026-09-10"):
        raise ConnectionError("连接被重置")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "拉取夜间 HRV" in message
    assert "连接被重置" in message
    assert "day=2026-09-10" in message  # 上下文可定位到具体哪一天


@pytest.mark.parametrize("exc", [
    TypeError("unsupported operand type(s) for /: 'NoneType' and 'int'"),
    AttributeError("'NoneType' object has no attribute 'get'"),
    NameError("name 'foo' is not defined"),
])
def test_swallowed_escalates_suspected_code_bugs_to_error(caplog, exc):
    """这三类异常几乎不可能由远端数据引起，出现即指向本项目自身缺陷。"""
    log = get_logger("test.obs")
    caplog.set_level(logging.DEBUG, logger="test.obs")
    with swallowed("拉取体重", logger=log):
        raise exc

    assert caplog.records[0].levelno == logging.ERROR
    assert "疑似代码缺陷" in caplog.records[0].getMessage()


@pytest.mark.parametrize("exc", [
    KeyError("weight"),
    ValueError("could not convert string to float"),
    OSError("read timed out"),
    ConnectionError("connection reset by peer"),
])
def test_swallowed_keeps_data_issues_at_warning(caplog, exc):
    """证据不足时不该报警成「代码缺陷」：KeyError 既可能是我们字段名写错，
    也可能是对方接口改了结构，两者的处置方式不同。"""
    log = get_logger("test.obs")
    caplog.set_level(logging.DEBUG, logger="test.obs")
    with swallowed("拉取分段", logger=log):
        raise exc

    assert caplog.records[0].levelno == logging.WARNING


def test_swallowed_is_quiet_on_success(caplog):
    """成功路径不得产生任何日志，否则正常的 60 天循环会把日志淹掉。"""
    log = get_logger("test.obs")
    caplog.set_level(logging.DEBUG, logger="test.obs")
    with swallowed("拉取分段", logger=log, activity="123"):
        pass
    assert caplog.records == []


# ============================================================ setup_logging
def test_setup_logging_is_idempotent():
    """uvicorn reload 与多入口重复调用不能叠加 handler，否则日志成倍输出。"""
    _reset_for_tests()
    root = logging.getLogger()
    before = len(root.handlers)
    try:
        setup_logging("INFO")
        after_first = len(root.handlers)
        setup_logging("DEBUG")
        assert after_first == before + 1
        assert len(root.handlers) == after_first
    finally:
        _reset_for_tests()


def test_setup_logging_falls_back_on_invalid_level():
    """日志配置本身不该成为启动失败的原因。"""
    _reset_for_tests()
    try:
        setup_logging("NOT_A_LEVEL")
        assert logging.getLogger().level == logging.INFO
    finally:
        _reset_for_tests()
        logging.getLogger().setLevel(logging.WARNING)


# ============================================================ 全仓护栏
def test_no_silently_swallowed_exception_in_app():
    """全仓不得再有「body 只有 pass」的 except —— 静默失效的唯一入口。

    精确统计（AST，不靠正则）：本轮开始时共 17 处，其中 14 处是第三方同步的
    单项降级（已改为记日志）、main.py 1 处非法路径（已记 debug）、
    evaluator.py 1 处不可达分支（已删除）、另 1 处为 main.py 的路径解析。
    """
    offenders = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) \
                    and all(isinstance(stmt, ast.Pass) for stmt in node.body):
                offenders.append(f"{py.relative_to(APP_DIR)}:{node.lineno}")
    assert not offenders, f"存在静默吞异常的 except 分支：{offenders}"


# ============================================================ 真实降级路径
class _AllFailingClient:
    """任意方法调用都抛网络异常，用于验证降级路径确实逐项留痕。"""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise ConnectionError(f"{name} 不可用")
        return _boom


class _WeighInOnlyClient:
    """只实现体重接口，其余抛异常；用于复现两个曾经的静默 bug。"""

    def __init__(self, payload):
        self.payload = payload

    def get_weigh_ins(self, *args, **kwargs):
        return self.payload

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise ConnectionError(f"{name} 不可用")
        return _boom


def _garmin_with_client(client):
    """构造 GarminAdapter 并替换 _client，绕开真实登录（本测试不联网）。"""
    from app.integrations.garmin import GarminAdapter

    adapter = GarminAdapter({})
    adapter._client = lambda: client
    return adapter


def test_garmin_body_metrics_logs_every_failed_item(caplog):
    """4 类指标 x 2 天全部失败 → 恰好 8 条告警，且每条都能定位到是哪天哪项。"""
    adapter = _garmin_with_client(_AllFailingClient())
    caplog.set_level(logging.WARNING, logger=GARMIN_LOGGER)

    assert adapter.fetch_body_metrics(days=2) == []

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 8, messages
    for action in ("拉取夜间 HRV", "拉取睡眠", "拉取当日统计", "拉取体重"):
        assert sum(action in m for m in messages) == 2, f"{action} 应每天记录一次"
    assert all("day=" in m for m in messages)


@pytest.mark.parametrize("payload", [
    None,                                   # 接口无数据时返回 None
    {},                                     # 空字典
    {"dateWeightList": [{}]},               # 记录存在但缺 weight 字段
    {"weightList": [{"weight": None}]},     # weight 显式为 null
    {"dateWeightList": []},                 # 列表为空
])
def test_garmin_weight_missing_data_does_not_raise(caplog, payload):
    """接口返回 None / 缺字段时不得抛 AttributeError / TypeError。

    修复前：``(w or {}).get(...) or w.get(...)`` 在 w 为 None 时抛
    AttributeError；``ws[-1].get("weight") / 1000`` 在缺字段时抛 TypeError
    （None / int）。两者都被静默吞掉，表现为体重字段永远缺失且无从察觉。
    """
    adapter = _garmin_with_client(_WeighInOnlyClient(payload))
    caplog.set_level(logging.DEBUG, logger=GARMIN_LOGGER)

    rows = adapter.fetch_body_metrics(days=1)

    assert all("weight_kg" not in row for row in rows)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"数据缺失不该被判定为代码缺陷：{errors}"


def test_garmin_weight_is_captured_when_present():
    """正向路径不能修坏：正常返回时体重须按克→千克换算。"""
    adapter = _garmin_with_client(_WeighInOnlyClient({"dateWeightList": [{"weight": 72500}]}))

    rows = adapter.fetch_body_metrics(days=1)

    assert rows and rows[0]["weight_kg"] == pytest.approx(72.5)


# ============================================================ 时间基准
def test_utcnow_naive_is_naive_utc_and_not_deprecated():
    """必须是「朴素 UTC」：带 tzinfo 会让新写入的字符串与历史行格式不一致。"""
    from app.models import utcnow_naive

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        got = utcnow_naive()          # 内部若用 datetime.utcnow() 会直接抛错

    assert got.tzinfo is None
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    assert abs((now_utc - got).total_seconds()) < 5


def test_no_deprecated_datetime_utcnow_in_app():
    """源码级护栏：datetime.utcnow 自 3.12 弃用、官方声明将移除。

    只认 AST 里的属性访问节点，因此文档里提到 ``datetime.utcnow()`` 的说明
    文字（字符串常量）不会被误判。
    """
    offenders = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                offenders.append(f"{py.relative_to(APP_DIR)}:{node.lineno}")
    assert not offenders, f"仍在使用已弃用的 datetime.utcnow：{offenders}"

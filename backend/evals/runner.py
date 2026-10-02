"""AI 教练评测的断言引擎（纯函数，不依赖模型，pytest 直接覆盖）。

与 tests/ 的分工：tests 锁「接线」（工具存在、闸门语料、护栏触发——用 monkeypatch
就能测），这里锁「质量」：真实模型在完整工具循环里的端到端行为。断言全部确定性
（工具调用序列、提案卡、error 事件、答案长度），不引入 LLM 评委——本地推理栈跑
评委又慢又不稳定，确定性断言失败时可定位到具体违规。

run_scenario 的 chat_fn 参数是注入点：跑批脚本传真模型的事件流生成器，
测试传假生成器，同一套断言逻辑两条路径共用。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

_REQUIRED_FIELDS = {"id", "category", "user_message", "expect"}
_EXPECT_FIELDS = {"must_call_tools", "must_call_tools_any", "must_not_call",
                  "must_emit_proposal", "allow_error", "min_answer_chars"}


def load_scenarios(path: str | Path) -> list[dict]:
    """读场景集并做 schema 校验：缺字段/重复 id/expect 非法一律响亮报错。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data["scenarios"] if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError("场景集为空或不是数组")
    seen: set[str] = set()
    for i, sc in enumerate(items):
        missing = _REQUIRED_FIELDS - sc.keys()
        if missing:
            raise ValueError(f"场景 #{i} 缺字段：{sorted(missing)}")
        unknown = set(sc["expect"]) - _EXPECT_FIELDS
        if unknown:
            raise ValueError(f"场景 {sc['id']} 的 expect 有未知断言：{sorted(unknown)}")
        if sc["id"] in seen:
            raise ValueError(f"场景 id 重复：{sc['id']}")
        seen.add(sc["id"])
    return items


def collect_trace(events: Iterable[dict]) -> dict:
    """从 chat_stream 的事件流收集断言所需的事实：工具调用、提案数、答案全文、错误。"""
    trace = {"tools": [], "proposals": 0, "answer": "", "errors": []}
    for ev in events:
        t = ev.get("type")
        if t == "tool":
            trace["tools"].append(ev.get("name") or ev.get("label") or "")
        elif t == "proposals":
            trace["proposals"] += len(ev.get("items") or [])
        elif t == "delta":
            trace["answer"] += ev.get("text") or ""
        elif t == "error":
            trace["errors"].append(ev.get("message") or "")
    return trace


def check_scenario(trace: dict, expect: dict) -> list[str]:
    """确定性断言：返回违规列表，空列表 = 通过。"""
    failures: list[str] = []
    for name in expect.get("must_call_tools", []):
        if name not in trace["tools"]:
            failures.append(f"必须调用的工具未调用：{name}")
    group = expect.get("must_call_tools_any", [])
    if group and not any(name in trace["tools"] for name in group):
        failures.append(f"应调用组内工具之一（{' / '.join(group)}），实际调用：{trace['tools'] or '无'}")
    for name in expect.get("must_not_call", []):
        if name in trace["tools"]:
            failures.append(f"禁止调用的工具被调用：{name}")
    want = expect.get("must_emit_proposal")
    if want is True and trace["proposals"] == 0:
        failures.append("改动/档案/目标类请求应产出提案卡，但整轮零提案（口头调整护栏未兜住）")
    if want is False and trace["proposals"] > 0:
        failures.append(f"纯查询场景不应产出提案卡（出了 {trace['proposals']} 张）")
    if not expect.get("allow_error", False) and trace["errors"]:
        failures.append(f"出现 error 事件：{trace['errors'][0]}")
    answer = trace["answer"].strip()
    min_chars = int(expect.get("min_answer_chars", 20))
    if len(answer) < min_chars:
        failures.append(f"答案过短（{len(answer)} < {min_chars} 字符），疑似空答或兜底文案")
    return failures


def run_scenario(chat_fn, scenario: dict) -> dict:
    """跑一个场景：chat_fn(user_message) -> 事件流生成器。"""
    events: Iterator[dict] = chat_fn(scenario["user_message"])
    trace = collect_trace(events)
    failures = check_scenario(trace, scenario["expect"])
    return {
        "id": scenario["id"], "category": scenario["category"],
        "pass": not failures, "failures": failures,
        "tools": trace["tools"], "proposals": trace["proposals"],
        "answer_chars": len(trace["answer"].strip()),
    }

"""评测断言引擎的锁定：场景 schema、确定性断言各分支、假模型全流程。

元测试（第一条）锁住 AGENTS.md 的教训：场景里引用的工具名必须真实存在于
TOOL_IMPLS——提示词/场景点名不存在的工具，模型照章调用只会拿到「未知工具」空转。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.services import ai_tools
from evals import runner

SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "scenarios.json"


def test_bundled_scenarios_valid_and_tools_exist():
    scenarios = runner.load_scenarios(SCENARIOS)
    assert len(scenarios) >= 20
    known = set(ai_tools.TOOL_IMPLS)
    for sc in scenarios:
        expect = sc["expect"]
        referenced = (list(expect.get("must_call_tools", []))
                      + list(expect.get("must_call_tools_any", []))
                      + list(expect.get("must_not_call", [])))
        unknown = [t for t in referenced if t not in known]
        assert not unknown, f"场景 {sc['id']} 引用了不存在的工具：{unknown}"
    categories = {sc["category"] for sc in scenarios}
    assert categories <= {"tool_discipline", "scope_gate", "proposal_guard",
                          "number_discipline"}


def test_load_scenarios_rejects_bad_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": "x", "category": "t", "user_message": "hi"}]',
                   encoding="utf-8")   # 缺 expect
    with pytest.raises(ValueError, match="缺字段"):
        runner.load_scenarios(bad)
    dup = tmp_path / "dup.json"
    dup.write_text('[{"id": "x", "category": "t", "user_message": "hi", "expect": {}},'
                   '{"id": "x", "category": "t", "user_message": "yo", "expect": {}}]',
                   encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        runner.load_scenarios(dup)


def _trace(tools=(), proposals=0, answer="足够长的正常教练回答内容，包含具体建议。", errors=()):
    return {"tools": list(tools), "proposals": proposals,
            "answer": answer, "errors": list(errors)}


def test_check_scenario_all_branches():
    ok = {"must_call_tools": ["a"], "must_call_tools_any": ["b", "c"],
          "must_emit_proposal": False}
    assert runner.check_scenario(_trace(tools=["a", "b"]), ok) == []

    missing = runner.check_scenario(_trace(tools=[]), ok)
    assert any("必须调用" in f for f in missing) and any("组内工具" in f for f in missing)

    forbidden = runner.check_scenario(_trace(tools=["a", "x"], proposals=1),
                                      {"must_not_call": ["x"], "must_emit_proposal": False})
    assert any("禁止调用" in f for f in forbidden)
    assert any("不应产出提案卡" in f for f in forbidden)

    needs_proposal = runner.check_scenario(_trace(), {"must_emit_proposal": True})
    assert any("零提案" in f for f in needs_proposal)

    errored = runner.check_scenario(_trace(errors=["模型超时"]), {})
    assert any("error 事件" in f for f in errored)
    assert runner.check_scenario(_trace(errors=["x"]), {"allow_error": True}) == []

    short = runner.check_scenario(_trace(answer="嗯"), {})
    assert any("答案过短" in f for f in short)


def test_run_scenario_with_fake_chat_fn():
    def good_chat(msg):
        yield {"type": "tool", "name": "get_this_week_workouts", "ok": True}
        yield {"type": "delta", "text": "这周安排了三节课：周二间歇、周四节奏、周日长距离。"}
        yield {"type": "done"}

    sc = {"id": "s1", "category": "tool_discipline", "user_message": "这周课表",
          "expect": {"must_call_tools": ["get_this_week_workouts"]}}
    r = runner.run_scenario(good_chat, sc)
    assert r["pass"] is True and r["failures"] == [] and r["tools"] == ["get_this_week_workouts"]

    def silent_chat(msg):
        yield {"type": "error", "message": "timed out"}
        yield {"type": "done"}

    r2 = runner.run_scenario(silent_chat, sc)
    assert r2["pass"] is False
    assert any("error 事件" in f for f in r2["failures"])
    assert any("答案过短" in f for f in r2["failures"])

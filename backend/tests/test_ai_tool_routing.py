"""工具路由：按意图裁剪工具集，把 33 个工具的静态开销降下来。

实测（2026-09-13，读 /v1/chat/completions 的 usage）：33 个工具 schema = 5041 token，
占 8K 窗口 62%；其中带参数的「知识库 + 档案/目标变更」两组占 3595 token（63.6%），
且各有独立触发语汇。裁掉它们能把静态开销降约四成，同时缩小 8B 模型的选择面。

本文件锁住的不只是「能省」，更重要的是**不能省错**：
查询类与课表变更类工具在任何路由下都必须全量在场——它们是主力场景，
提示词还要求模型对疲劳/酸痛主动出提案，缺了手就是能力倒退。
"""
from __future__ import annotations

import json

import pytest
from app.services import ai_coach, ai_tools
from test_ai_tool_loop import FakeClient, _install, _text_chunk, _tool_chunk

ALL_NAMES = [t["function"]["name"] for t in ai_tools.TOOLS_SCHEMA]
OPTIONAL_NAMES = set(ai_coach.KNOWLEDGE_TOOLS) | set(ai_coach.PROFILE_GOAL_TOOLS)
ALWAYS_ON = [n for n in ALL_NAMES if n not in OPTIONAL_NAMES]


@pytest.fixture(autouse=True)
def _force_routing_on(monkeypatch):
    """路由默认已改为按窗口自动（32768 窗口下关闭）；本文件测的是路由机制本身，
    除个别用例外统一强制开启。"""
    monkeypatch.setattr(ai_coach, "TOOL_ROUTING_ENABLED", True)


def _names(schema: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in schema]


def _route(*messages: str) -> tuple[list[dict], str]:
    return ai_coach.route_tool_schema([{"role": "user", "content": m} for m in messages])


# ---------------------------------------------------------------- 1. 分组本身的约束

def test_optional_groups_exist_and_are_disjoint():
    assert len(OPTIONAL_NAMES) == 11, f"可选组应是 8 个知识库 + 3 个档案目标，实际 {len(OPTIONAL_NAMES)}"
    assert set(ALL_NAMES) >= OPTIONAL_NAMES, "分组里出现了 schema 中不存在的工具名"
    assert not (set(ai_coach.KNOWLEDGE_TOOLS) & set(ai_coach.PROFILE_GOAL_TOOLS))


def test_always_on_set_is_the_big_majority_of_the_workhorses():
    """查询类与课表提案类必须留在常驻集里（这是路由的安全底线）。"""
    assert "get_athlete_profile" in ALWAYS_ON
    assert "get_recovery_status" in ALWAYS_ON
    assert "get_this_week_workouts" in ALWAYS_ON
    for name in ("propose_move_workout", "propose_quality_adjustment",
                 "propose_easy_replacement", "propose_add_workout", "propose_skip_workout"):
        assert name in ALWAYS_ON, f"{name} 是提示词铁律要求的主动提案工具，不能被路由裁掉"


# ---------------------------------------------------------------- 2. 裁剪行为

def test_in_domain_query_gets_the_resident_set():
    """没命中任何分组时下发常驻集——这才是省下窗口的常规路径。"""
    schema, note = _route("我最近状态怎么样")
    assert _names(schema) == ALWAYS_ON, "默认应下发常驻集（顺序保持原样）"
    assert len(ALWAYS_ON) < len(ALL_NAMES), "常驻集本身就该比全量小"
    assert "常驻集" in note


def test_knowledge_question_adds_knowledge_tools():
    schema, note = _route("汉森训练法适合我这水平吗")
    names = set(_names(schema))
    assert set(ai_coach.KNOWLEDGE_TOOLS) <= names
    assert set(ALWAYS_ON) <= names, "命中分组只能是「加」，不能把常驻集换掉"
    assert not (set(ai_coach.PROFILE_GOAL_TOOLS) & names), "不该顺带把档案/目标组也塞进来"
    assert len(names) < len(ALL_NAMES)
    assert "知识库" in note


def test_profile_change_adds_profile_tools():
    names = set(_names(_route("我体重 85 公斤了")[0]))
    assert "propose_profile_update" in names
    assert "search_training_methods" not in names


def test_goal_change_adds_goal_tools():
    names = set(_names(_route("我想改跑半马，比赛定在十月")[0]))
    assert {"propose_goal_update", "precheck_goal"} <= names
    assert "propose_profile_update" not in names


def test_both_groups_can_be_selected_together():
    names = set(_names(_route("我体重涨了，想按汉森训练法重新定个全马目标")[0]))
    assert set(ai_coach.KNOWLEDGE_TOOLS) <= names
    assert set(ai_coach.PROFILE_GOAL_TOOLS) <= names


def test_follow_up_turn_keeps_previous_user_turn_context():
    """追问常常省掉主题词（「那帮我试试」），只看本轮会把知识库工具裁掉。"""
    schema, _ = ai_coach.route_tool_schema([
        {"role": "user", "content": "汉森训练法适合我吗"},
        {"role": "assistant", "content": "它的核心是累积疲劳……"},
        {"role": "user", "content": "那帮我试试这个"},
    ])
    assert set(ai_coach.KNOWLEDGE_TOOLS) <= set(_names(schema))


def test_routing_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ai_coach, "TOOL_ROUTING_ENABLED", False)
    schema, note = _route("汉森训练法适合我这水平吗")
    assert _names(schema) == ALL_NAMES
    assert "关闭" in note


def test_empty_history_falls_back_to_full_schema():
    schema, _ = ai_coach.route_tool_schema([])
    assert _names(schema) == ALL_NAMES


# ---------------------------------------------------------------- 3. 安全底线的穷举

CORPUS = [
    "我最近状态怎么样", "帮我把周二的间歇挪到周四", "帮我看看同步过来的数据",
    "这周的课表发我看看", "我今天的配速该跑多少", "我昨天没睡好，今天还能跑间歇吗",
    "帮我改一下课表，把周四那节换成轻松跑", "我的体重是不是掉太快了",
    "我下个月有半马比赛，现在练得够吗", "帮我分析下我的恢复状态",
    "打卡里我写了膝盖有点疼，要紧吗", "我想加一次有氧，安排在周几合适",
    "汉森训练法适合我这水平吗", "我的静息心率最近变高了", "推荐一个适合我的计划",
    "把周四的间歇减两组", "今天该吃什么", "今天太累了", "这周负荷合理吗",
    "请你去同步一下我在高驰上的课表", "帮我买双跑鞋", "把课表发我微信",
]


@pytest.mark.parametrize("text", CORPUS)
def test_workhorse_tools_survive_every_route(text):
    """无论命中哪个分组，常驻工具都必须一个不少。"""
    names = set(_names(_route(text)[0]))
    missing = set(ALWAYS_ON) - names
    assert not missing, f"「{text}」的路由把常驻工具裁掉了：{sorted(missing)}"


def test_routed_schema_keeps_original_order():
    """顺序必须是原 schema 的子序列——别给模型引入无谓的行为变量。"""
    schema, _ = _route("汉森训练法适合我吗，顺便看看我体重")
    routed = _names(schema)
    assert routed == [n for n in ALL_NAMES if n in set(routed)], "路由后顺序被打乱了"


# ---------------------------------------------------------------- 4. 省下的窗口要真的还给动态内容

def test_routing_actually_shrinks_the_schema():
    """常规问答（未命中语汇）必须省下大头，否则这套复杂度不值得引入。

    注意要按**默认路径**衡量：命中知识库时省下的只是档案/目标组（最便宜的那部分），
    而绝大多数请求走的是默认路径。
    """
    full = ai_coach._schema_tokens(ai_tools.TOOLS_SCHEMA)
    default = ai_coach._schema_tokens(_route("我最近状态怎么样")[0])
    knowledge = ai_coach._schema_tokens(_route("汉森训练法适合我这水平吗")[0])
    assert full - default > 2000, f"默认路径裁剪幅度太小（全量 {full} / 常驻集 {default}）"
    assert full - knowledge > 900, f"命中知识库时也应至少省掉档案/目标组（{knowledge}）"
    assert ai_coach._active_budget(default) > ai_coach._active_budget(full), \
        "省下的静态开销必须还给预算，否则裁剪白做"
    # 动态预算必须真的宽裕起来，而不只是数字好看
    assert ai_coach._active_budget(default) >= 3000, \
        f"默认路径下动态预算仍只有 {ai_coach._active_budget(default)} token，太紧"


def test_loop_sends_the_routed_schema(monkeypatch):
    """接线验证：模型真正收到的 tools 是裁剪后的那一份，而不是全量。"""
    client = FakeClient(tool_rounds=[[_tool_chunk("search_training_methods")],
                                     [_text_chunk("汉森的核心是累积疲劳。")]])
    executed: list = []
    _install(monkeypatch, client, executed)

    list(ai_coach.chat_stream(None, [{"role": "user", "content": "汉森训练法适合我吗"}]))

    assert client.tool_requests, "应发起过带工具的请求"
    sent = json.loads(json.dumps(client.tool_requests[0]["tools"], ensure_ascii=False))
    sent_names = [t["function"]["name"] for t in sent]
    assert len(sent_names) < len(ALL_NAMES), "下发的应是裁剪后的工具集"
    assert "search_training_methods" in sent_names
    assert "propose_profile_update" not in sent_names


def test_loop_hands_the_routed_budget_to_trimming(monkeypatch):
    """裁剪用的预算必须按**本轮实际下发的 schema** 推，而不是模块级常量。

    若图省事把 CONTEXT_TOKEN_BUDGET（按全量算）传进去，路由省下的窗口就白留了，
    还会把本来放得下的历史误裁掉——这个接线错误光看路由函数是发现不了的。
    """
    seen: list[int] = []
    real_trim = ai_coach._trim_context

    def spy(msgs, budget, dropped_out=None):
        seen.append(budget)
        return real_trim(msgs, budget, dropped_out=dropped_out)

    monkeypatch.setattr(ai_coach, "_trim_context", spy)
    client = FakeClient(tool_rounds=[[_tool_chunk("get_recovery_status")],
                                     [_text_chunk("你恢复得不错。")]])
    _install(monkeypatch, client, [])

    list(ai_coach.chat_stream(None, [{"role": "user", "content": "我最近状态怎么样"}]))

    expected = ai_coach._active_budget(
        ai_coach._schema_tokens(_route("我最近状态怎么样")[0]))
    assert seen, "工具轮必须走到裁剪这一步"
    assert all(b == expected for b in seen), f"实际传入 {seen}，应为 {expected}"
    assert expected > ai_coach.CONTEXT_TOKEN_BUDGET, \
        "常驻集下预算必须比「按全量算」的常量更宽，否则路由的收益被吃掉了"

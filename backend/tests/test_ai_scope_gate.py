"""能力闸门：确定性判定「能力外请求」，注入指引并约束本轮回答。

为什么要代码闸门而不是只靠提示词：提示词里的能力边界靠模型自觉，8B 并不总能自觉。
闸门把「能不能做」这个判断从模型手里拿走（确定性），模型只负责把已判定的指引说成人话。

命中后的处置（2026-09-28 起）：指引作为 system 注记注入本轮消息，常驻工具**照常下发**——
用户一句话常混着在域部分（「帮我同步下，顺便分析下我的恢复」），硬断粮会让在域部分
也答不了；执行类诉求由注入的指引明确拒绝，模型硬拿查询工具「试探」时由
重复短路/停滞早停兜住。收尾请求仍禁用工具。

判定必须带**祈使信号**（请/帮我/怎么… 或 动词+一下）才算命中，否则会把
「同步过来的数据」这类在域描述误判成同步请求——下面那份在域语料就是这套正则的护栏，
改正则必须同时跑它，否则闸门会开始吞掉正常提问。
"""
from __future__ import annotations

import pytest
from app.services import ai_coach

# import 同目录的假模型服务装置（pytest 以 rootdir/tests 为 sys.path 前缀，可直接 import）
from test_ai_tool_loop import FakeClient, _deltas, _install, _text_chunk, _tool_chunk

# ---------------------------------------------------------------- 1. 能力外请求必须命中

@pytest.mark.parametrize("text,marker", [
    # 复现 2026-09-13 那次失败的原始请求
    ("请你去同步一下我在高驰上的课表，同步到这个软件的训练计划里", "设置 → 平台连接"),
    ("帮我连接一下高驰账号", "设置 → 平台连接"),
    ("怎么把课表导入训练计划", "反向导入"),
    ("帮我把课表同步到手表", "下发手表"),
    ("数据同步成功了吗", "设置 → 平台连接"),
    ("帮我安装一下佳明插件", "安装"),
    ("怎么下载 FIT 文件", "安装"),
    ("帮我买双跑鞋", "购物"),
    ("把课表发我微信", "微信"),
    ("帮我改一下 .env 里的配置", "本机"),
])
def test_out_of_scope_requests_are_detected(text, marker):
    guidance = ai_coach.detect_out_of_scope(text)
    assert guidance, f"这条应被判定为能力外：{text}"
    assert marker in guidance, f"指引里应指出「{marker}」，实际：{guidance}"


# ---------------------------------------------------------------- 2. 在域消息绝不能被误伤

IN_DOMAIN = [
    "我最近状态怎么样",
    "帮我把周二的间歇挪到周四",
    "帮我看看同步过来的数据",          # ← 陷阱：描述「已同步」，不是发起同步
    "同步过来的数据我想看看",            # ← 同上
    "这周的课表发我看看",               # ← 陷阱：发我 ≠ 对外发消息
    "我上次把数据同步到手表了",          # ← 陷阱：过去时叙述，不是请求
    "我今天的配速该跑多少",
    "帮我看看这周跑了多少",
    "我昨天没睡好，今天还能跑间歇吗",
    "帮我改一下课表，把周四那节换成轻松跑",   # ← 陷阱：改课表 ≠ 改配置
    "我的体重是不是掉太快了",
    "我下个月有半马比赛，现在练得够吗",
    "帮我分析下我的恢复状态",
    "这周的训练负荷还合理吗",
    "打卡里我写了膝盖有点疼，要紧吗",
    "我想加一次有氧，安排在周几合适",
    "汉森训练法适合我这水平吗",
    "我的静息心率最近变高了",
    "推荐一个适合我的计划",
    "把周四的间歇减两组",
    "今天该吃什么",
    "帮我看看买什么跑鞋合适",          # ← 装备咨询是在域的，只有「帮我买」才算购物请求
    "帮我推荐一双跑鞋",
]


@pytest.mark.parametrize("text", IN_DOMAIN)
def test_in_domain_messages_are_never_gated(text):
    """误伤的代价是「用户正常提问被回一句我做不到」，比漏判严重得多。"""
    assert ai_coach.detect_out_of_scope(text) is None, \
        f"在域消息被闸门误判：{text} → {ai_coach.detect_out_of_scope(text)}"


def test_empty_and_blank_text_is_not_gated():
    for text in ("", "   ", "\n"):
        assert ai_coach.detect_out_of_scope(text) is None


# ---------------------------------------------------------------- 3. 闸门与循环的接线

_ORIGINAL_REQUEST = "请你去同步一下我在高驰上的课表，同步到这个软件的训练计划里"


def test_gate_guidance_injected_and_no_tools_executed(monkeypatch):
    """闸门命中：指引注入本轮消息，模型直接作答 → 不执行任何工具，产出指路文案而非报错。"""
    client = FakeClient(tool_rounds=[], final=[_text_chunk(ai_coach.PLATFORM_GUIDANCE)])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": _ORIGINAL_REQUEST}]))

    assert executed == [], "闸门命中时不得执行任何工具"
    assert len(client.finalize_requests) == 1, "必须发起恰好一次无工具收尾请求"
    assert "平台连接" in _deltas(events)
    assert not [e for e in events if e["type"] == "error"], "有指引就不该再报错"
    # 指引必须真的注入了本轮消息——混合请求时模型靠它知道执行类诉求被拒绝
    assert any(m.get("role") == "system" and "平台连接" in str(m.get("content"))
               for m in client.calls[0]["messages"])


def test_gate_mixed_request_still_answers_the_in_domain_part(monkeypatch):
    """混合请求（执行类 + 在域查询）：执行类被指引拒绝，在域部分照常查数据回答。
    这是「命中即断粮」改「注入指引 + 保留工具」的核心场景。"""
    client = FakeClient(
        tool_rounds=[[_tool_chunk("get_recovery_status")]],
        final=[_text_chunk("同步我做不了，请到「设置 → 平台连接」页操作；你的恢复状态不错。")])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content":
                   "帮我把数据同步一下，顺便分析下我的恢复状态"}]))

    assert executed == [("get_recovery_status", {})], "在域部分必须照常走工具"
    assert "平台连接" in _deltas(events), "执行类诉求的指引必须出现在回答里"
    assert not [e for e in events if e["type"] == "error"]


def test_gate_falls_back_to_guidance_when_model_says_nothing(monkeypatch):
    """模型收尾也没产出内容时，直接用指引原文作答——它本身就是可执行的回答，不该降级成 error。"""
    client = FakeClient(tool_rounds=[], final=[])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": _ORIGINAL_REQUEST}]))

    assert _deltas(events) == ai_coach.PLATFORM_GUIDANCE
    assert not [e for e in events if e["type"] == "error"], "不应把能力缺口包装成错误"
    assert events[-1]["type"] == "done"


def test_gate_note_survives_tool_routing(monkeypatch):
    """闸门先于路由注入指引：即使问题里带知识库语汇、路由给了知识库工具，
    指引也必须注入本轮消息（工具可以给，被拒绝的诉求要说清）。"""
    monkeypatch.setattr(ai_coach, "TOOL_ROUTING_ENABLED", True)
    client = FakeClient(tool_rounds=[], final=[_text_chunk(ai_coach.PLATFORM_GUIDANCE)])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(
        None, [{"role": "user", "content": "帮我同步一下汉森训练法的课表"}]))

    assert executed == [], "被拒绝的同步诉求不得真的执行任何工具"
    assert "平台连接" in _deltas(events)
    assert any(m.get("role") == "system" and "平台连接" in str(m.get("content"))
               for m in client.calls[0]["messages"])


def test_in_domain_request_still_uses_the_tool_loop(monkeypatch):
    """反向护栏：闸门不得把正常提问也拦到无工具路径上。"""
    client = FakeClient(tool_rounds=[[_tool_chunk("get_recovery_status")],
                                     [_text_chunk("你恢复得不错。")]])
    executed: list = []
    _install(monkeypatch, client, executed)

    list(ai_coach.chat_stream(None, [{"role": "user", "content": "帮我分析下我的恢复状态"}]))

    assert executed == [("get_recovery_status", {})], "在域请求必须照常走工具"


def test_boundary_instruction_carries_the_hint():
    """收尾指令必须把指引嵌进去，并要求不得删改结论与指路。"""
    ins = ai_coach.FINALIZE_BOUNDARY_INSTRUCTION.format(hint="【指引】")
    assert "【指引】" in ins
    assert "不要再要求调用工具" in ins
    assert "不得删改" in ins

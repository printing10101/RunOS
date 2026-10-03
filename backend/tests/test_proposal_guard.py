"""口头调整护栏测试（2026-09-28「推荐课表只复述不出提案」问题的回归锁定）。

背景：用户请教练「推荐这周的课表和相应的配速」，模型查了 6 个查询工具后只在
回答里口头给出一套「可调低周日20km配速至7:20/km」——零提案，用户拿到一段改不了
课表的话。根因是提案铁律挂在「用户要求调整课表」上盖不住「推荐/优化」，且铁律
只是提示词纪律、没有确定性兜底。

本文件锁定两层修复：
1. 确定性护栏：答案命中改动措辞 / 用户消息命中档案·目标变更而整轮零提案 →
   追加最多两轮非流式「提案转换」，把口头改动落成提案卡片；任何失败原样放行；
2. 提示词：铁律 8 显式盖住「推荐/优化/评估课表」请求（无改动则明确说维持原样）。

全部用假模型服务与假工具层，不碰真实库、不依赖 llama-server。
"""
import copy
import re
from types import SimpleNamespace

from app.config import settings
from app.services import ai_coach, ai_tools
from test_ai_tool_loop import FakeClient, _text_chunk, _tool_chunk

# ---------------------------------------------------------------- 假模型服务（补 client.post）

class _FakePostResponse:
    def __init__(self, message: dict):
        self._message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": self._message}]}


class GuardClient(FakeClient):
    """在 FakeClient 上补 post：提案转换轮走非流式请求，与其余流式轮分流。"""

    def __init__(self, *args, post_responses=None, post_error: Exception | None = None, **kw):
        super().__init__(*args, **kw)
        self.post_calls: list[dict] = []
        self._post_responses = list(post_responses or [])
        self._post_error = post_error

    def post(self, url, headers=None, json=None):
        self.post_calls.append(copy.deepcopy(json))
        if self._post_error:
            raise self._post_error
        idx = min(len(self.post_calls) - 1, len(self._post_responses) - 1)
        return _FakePostResponse(self._post_responses[idx] if self._post_responses else {})


def _post_msg(name: str, args: str, call_id: str = "guard_1") -> dict:
    return {"content": "", "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": args}}]}


def _install(monkeypatch, client: GuardClient, executed: list):
    """与 test_ai_tool_loop._install 同构，但 propose_* 会返回 proposal（护栏靠它收卡片）。"""
    real = ai_coach.httpx
    monkeypatch.setattr(ai_coach, "probe", lambda: {
        "reachable": True, "model": "mock", "base_url": "http://127.0.0.1:1/v1",
        "models": ["mock"], "model_ready": True})
    monkeypatch.setattr(ai_coach, "system_prompt", lambda db: "SYS")
    monkeypatch.setattr(ai_coach, "httpx", SimpleNamespace(
        Client=lambda **kw: client,
        Timeout=real.Timeout,
        ConnectError=real.ConnectError,
        ReadTimeout=real.ReadTimeout,
        RemoteProtocolError=real.RemoteProtocolError))
    monkeypatch.setattr(ai_tools, "_get_athlete", lambda db: SimpleNamespace(id=1))

    def fake_execute(db, athlete, name, args):
        executed.append((name, args))
        if name.startswith("propose_"):
            return {"ok": True, "tool": name, "proposal": {"id": 7, "kind": name}}
        return {"ok": True, "tool": name}

    monkeypatch.setattr(ai_tools, "execute_tool", fake_execute)


def _deltas(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


# 截图原话（2026-09-28）：模型口头调整、零提案的实锤样本
_VERBAL_ANSWER = ("根据你的目标（半马1:30:00）和当前状态，本周训练框架如下：\n"
                  "⚠ 关键调整建议：\n"
                  "3. 负荷平衡：本周轻松跑占比偏低，周四6km必须完成，"
                  "可调低周日20km配速至7:20/km（引擎计算值）")


# ---------------------------------------------------------------- 1. 检测正则

def test_schedule_change_regex_hits_verbal_tweaks():
    hits = [
        "可调低周日20km配速至7:20/km（引擎计算值）",
        "建议把周六的质量课换成轻松跑",
        "配速可放宽至7:20/km",
        "如果觉得累，可以把间歇减到4组",
        "不妨把长距离放到周日",
        "周四那节课跳过吧",
        "周日长距离提前到周六",
    ]
    for t in hits:
        assert ai_coach._SCHEDULE_CHANGE_RE.search(t), f"应命中口头改动：{t}"


def test_schedule_change_regex_ignores_plain_description():
    misses = [
        "本周课表维持原样：周日长距离 20km，配速区间为 6:51-7:29。",
        "马拉松配速(M) 6:51-7:29；轻松跑(E) 7:34-8:59。",
        "你的 VDOT 是 28.8，各课型配速由引擎按此计算。",
        "建议保证睡眠，训练前注意碳水摄入。",
    ]
    for t in misses:
        assert not ai_coach._SCHEDULE_CHANGE_RE.search(t), f"描述现状不应命中：{t}"


def test_profile_and_goal_regex_on_user_text():
    assert ai_coach._PROFILE_CHANGE_RE.search("我体重85kg了")
    assert ai_coach._PROFILE_CHANGE_RE.search("最大心率195")
    assert ai_coach._PROFILE_CHANGE_RE.search("我叫老李")
    assert not ai_coach._PROFILE_CHANGE_RE.search("体重对配速的影响大吗")
    assert ai_coach._GOAL_CHANGE_RE.search("我想把目标改成全马330")
    assert ai_coach._GOAL_CHANGE_RE.search("今年想破三")
    assert ai_coach._GOAL_CHANGE_RE.search("想跑个半马")
    assert not ai_coach._GOAL_CHANGE_RE.search("我的目标现在该配什么速")


def test_unenforced_change_kinds_respects_called_tools():
    assert ai_coach._unenforced_change_kinds(
        "推荐这周课表", "可调低周日20km配速至7:20/km", set()) == ["课表调整"]
    assert ai_coach._unenforced_change_kinds(
        "我体重85kg了", "好的，已记下。", set()) == ["档案更新"]
    assert ai_coach._unenforced_change_kinds(
        "目标改成全马", "好的。", set()) == ["目标变更"]
    # 已有提案（或已调过对应提案工具）＝不缺强制力，不再触发
    assert ai_coach._unenforced_change_kinds(
        "推荐这周课表", "可调低周日20km配速至7:20/km",
        {"propose_easy_replacement"}) == []


# ---------------------------------------------------------------- 2. 提示词与工具真实性

def test_system_prompt_covers_recommend_schedule_requests():
    """「推荐/优化课表」必须被提案铁律盖住：有改动落提案，无改动明说维持原样。"""
    sp = ai_coach.SYSTEM_PROMPT
    assert "推荐/优化/评估这周课表" in sp
    assert "本周课表维持原样" in sp


def test_guard_only_references_real_tools():
    """护栏 schema 与转换指令点名的工具必须真实存在（防止「鸡肋工具」复发）。"""
    assert ai_coach.GUARD_TOOL_NAMES <= set(ai_tools.TOOL_IMPLS)
    for m in re.findall(r"[a-z][a-z_]{3,}", ai_coach.GUARD_CONVERT_INSTRUCTION):
        if m.startswith(("propose_", "get_", "precheck_")):
            assert m in ai_tools.TOOL_IMPLS, f"转换指令引用了不存在的工具：{m}"
    assert ai_coach._GUARD_SCHEMA, "护栏 schema 不能为空"


# ---------------------------------------------------------------- 3. 端到端：正常路径的护栏

def test_verbal_schedule_change_gets_converted_to_proposal(monkeypatch):
    """截图场景回归：答案口头调低配速、零提案 → 转换轮补出提案卡片。"""
    client = GuardClient(
        tool_rounds=[[_text_chunk(_VERBAL_ANSWER)]],
        post_responses=[_post_msg("propose_easy_replacement", '{"workout_id": 12}')])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(
        None, [{"role": "user", "content": "请根据我的目标和身体状况给我推荐这周的课表和相应的配速"}]))

    assert not [e for e in events if e["type"] == "error"]
    # 原回答一字不少地交付，且转换轮是非流式——不给用户叠第二遍正文
    assert _VERBAL_ANSWER in _deltas(events)
    assert _deltas(events).count("调低周日20km配速") == 1
    # 提案确实补出来了
    assert ("propose_easy_replacement", {"workout_id": 12}) in executed
    proposals = [e for e in events if e["type"] == "proposals"]
    assert proposals and proposals[0]["items"] == [{"id": 7, "kind": "propose_easy_replacement"}]
    tool_evts = [e for e in events if e["type"] == "tool"]
    assert any("提案补齐" in e["label"] for e in tool_evts)
    # 转换指令真的带上了上一条回答 + 只下发护栏工具集
    payload = client.post_calls[0]
    assert payload["messages"][-1]["content"].startswith("系统复核")
    assert any(m.get("role") == "assistant" and "调低周日20km" in (m.get("content") or "")
               for m in payload["messages"]), "转换轮必须能看到被复核的回答"
    sent_names = {t["function"]["name"] for t in payload["tools"]}
    assert sent_names == ai_coach.GUARD_TOOL_NAMES
    assert len(client.post_calls) == 1, "提案到手后不得再烧第二轮转换请求"


def test_clean_answer_skips_guard(monkeypatch):
    """「维持原样 + 现状描述」不应触发转换轮。"""
    client = GuardClient(
        tool_rounds=[[_text_chunk("本周课表维持原样，周日长距离 20km，配速区间为 6:51-7:29。")]])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(
        None, [{"role": "user", "content": "推荐一下这周的课表"}]))

    assert client.post_calls == []
    assert not [e for e in events if e["type"] == "proposals"]
    assert not [e for e in events if e["type"] == "error"]


def test_guard_skipped_when_proposals_already_made(monkeypatch):
    """主循环已出提案（模型守了纪律）→ 护栏不再叠加转换轮。"""
    client = GuardClient(
        tool_rounds=[[_tool_chunk("propose_easy_replacement", '{"workout_id": 3}')],
                     [_text_chunk("已把周日长距离替换为轻松跑，等你确认。")]])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "周日太累，帮我换成轻松跑"}]))

    assert client.post_calls == []
    proposals = [e for e in events if e["type"] == "proposals"]
    assert proposals and len(proposals[0]["items"]) == 1


def test_guard_disabled_by_flag(monkeypatch):
    """AI_PROPOSAL_GUARD=false 时完全旁路（急停开关）。"""
    monkeypatch.setattr(settings, "ai_proposal_guard", False)
    client = GuardClient(tool_rounds=[[_text_chunk(_VERBAL_ANSWER)]])
    _install(monkeypatch, client, [])

    events = list(ai_coach.chat_stream(
        None, [{"role": "user", "content": "推荐这周的课表和配速"}]))

    assert client.post_calls == []
    assert not [e for e in events if e["type"] == "proposals"]
    assert _VERBAL_ANSWER in _deltas(events)


def test_guard_failure_never_breaks_the_answer(monkeypatch):
    """转换轮请求挂掉：原回答照常交付，绝不产出第二个 error 路径。"""
    client = GuardClient(
        tool_rounds=[[_text_chunk(_VERBAL_ANSWER)]],
        post_error=ConnectionError("model went away"))
    _install(monkeypatch, client, [])

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "推荐这周的课表和配速"}]))

    assert not [e for e in events if e["type"] == "error"]
    assert not [e for e in events if e["type"] == "proposals"]
    assert _VERBAL_ANSWER in _deltas(events)


def test_profile_change_in_user_text_triggers_guard(monkeypatch):
    """用户亲口报档案变化而教练只口头应下 → 转换轮补档案提案。"""
    client = GuardClient(
        tool_rounds=[[_text_chunk("好的，已记下你的新体重85kg，我会据此调整建议。")]],
        post_responses=[_post_msg("propose_profile_update", '{"weight_kg": 85}')])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "我体重85kg了，最近练得怎么样"}]))

    assert ("propose_profile_update", {"weight_kg": 85}) in executed
    proposals = [e for e in events if e["type"] == "proposals"]
    assert proposals and proposals[0]["items"] == [{"id": 7, "kind": "propose_profile_update"}]


def test_guard_two_rounds_query_then_propose(monkeypatch):
    """转换轮先想再查 workout_id（重复短路喂提示）→ 第二轮出提案，最多两轮。"""
    client = GuardClient(
        tool_rounds=[[_tool_chunk("get_this_week_workouts")],
                     [_text_chunk(_VERBAL_ANSWER)]],
        post_responses=[
            _post_msg("get_this_week_workouts", "{}", call_id="guard_1"),
            _post_msg("propose_easy_replacement", '{"workout_id": 12}', call_id="guard_2")])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "推荐这周的课表和配速"}]))

    # get_this_week_workouts 主循环已查过：转换轮里的重复调用被短路，不再真实执行
    assert executed.count(("get_this_week_workouts", {})) == 1
    assert ("propose_easy_replacement", {"workout_id": 12}) in executed
    assert len(client.post_calls) == 2
    repeat_feed = client.post_calls[1]["messages"][-1]
    assert repeat_feed["role"] == "tool"
    assert "已经调用过" in repeat_feed["content"]
    proposals = [e for e in events if e["type"] == "proposals"]
    assert proposals and proposals[0]["items"] == [{"id": 7, "kind": "propose_easy_replacement"}]


# ---------------------------------------------------------------- 4. 端到端：收尾路径的护栏

def test_finalize_answer_also_guarded(monkeypatch):
    """触顶后的收尾作答同样过护栏：口头调整也要落成卡片。"""
    monkeypatch.setattr(ai_coach, "MAX_TOOL_ROUNDS", 2)
    scans = [[_tool_chunk(name, call_id=f"call_{i}")] for i, name in enumerate(
        ["get_athlete_profile", "get_training_status"])]
    client = GuardClient(
        tool_rounds=scans,
        final=[_text_chunk("状态一般，可以把周日的配速放宽到 7:20/km 再跑。")],
        post_responses=[_post_msg("propose_quality_adjustment", '{"workout_id": 5}')])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "帮我分析下我的恢复状态"}]))

    assert not [e for e in events if e["type"] == "error"]
    assert ("propose_quality_adjustment", {"workout_id": 5}) in executed
    proposals = [e for e in events if e["type"] == "proposals"]
    assert proposals and proposals[0]["items"] == [{"id": 7, "kind": "propose_quality_adjustment"}]


def test_finalize_answer_skips_guard_on_boundary_hint(monkeypatch):
    """能力闸门命中时收尾讲的是「做不到 + 去哪手动做」，与提案转换打架 → 不叠加。"""
    monkeypatch.setattr(ai_coach, "MAX_TOOL_ROUNDS", 1)
    client = GuardClient(
        tool_rounds=[[_tool_chunk("get_athlete_profile")]],
        final=[_text_chunk("我做不到同步数据。另外，可以把周日20km配速调低至7:20/km。")])
    _install(monkeypatch, client, [])

    events = list(ai_coach.chat_stream(
        None, [{"role": "user", "content": "请帮我同步一下高驰的训练数据，顺便推荐这周课表"}]))

    assert client.post_calls == [], "闸门命中的收尾轮不得再叠加提案转换"
    assert not [e for e in events if e["type"] == "proposals"]
    assert not [e for e in events if e["type"] == "error"]


# ---------------------------------------------------------------- 5. 不干扰既有正常路径

def test_normal_in_domain_answer_unaffected(monkeypatch):
    """常规问答（无改动措辞）：不 post、不提案，行为与护栏上线前一致。"""
    client = GuardClient(
        tool_rounds=[[_tool_chunk("get_recovery_status")],
                     [_text_chunk("你恢复得不错，按课表练即可。")]])
    executed: list = []
    _install(monkeypatch, client, executed)

    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "帮我分析下我的恢复状态"}]))

    assert executed == [("get_recovery_status", {})]
    assert client.post_calls == []
    assert _deltas(events) == "你恢复得不错，按课表练即可。"
    assert not [e for e in events if e["type"] == "error"]

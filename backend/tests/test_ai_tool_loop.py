"""AI 教练工具循环的收敛与兜底测试。

背景（2026-09-13 复盘）：用户说「请你去同步一下我在高驰上的课表」，AI 连调 6 个
与意图无关的查询工具后触顶，只吐一句「已达最大工具轮数（6），请换个问法」——
零回答，还把锅甩回给用户。根因是工具集无对应能力 + 提示词无能力边界 + 循环无收敛。

本文件锁定修复后的三条护栏：
1. 重复调用（同工具同参数）→ 第二次起直接短路，不执行也不重复喂结果；
2. 连续多轮「零个新工具」→ 提前收尾，不白烧剩余轮数；
3. 轮数用尽/停滞 → 禁用工具做一次收尾作答，用户必须拿到一句人话而不是光秃秃的报错。

全部用假模型服务与假工具层，不碰真实库、不依赖 llama-server。
"""
import copy
from types import SimpleNamespace

from app.services import ai_coach, ai_tools

# ---------------------------------------------------------------- 假模型服务

def _tool_chunk(name: str, args: str = "{}", call_id: str = "call_1", index: int = 0) -> dict:
    return {"choices": [{"delta": {"tool_calls": [
        {"index": index, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": args}}]}}]}


def _text_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


class _FakeResponse:
    def __init__(self, chunks: list[dict], status: int = 200):
        self.status_code = status
        self._lines = [f"data: {_dumps(c)}" for c in chunks]
        if status == 200:
            self._lines.append("data: [DONE]")

    def iter_lines(self):
        yield from self._lines

    def read(self) -> bytes:
        return b'{"error": "mock failure"}'


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


class _FakeStream:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class FakeClient:
    """按请求是否携带 tools 分流：带 tools → tool_rounds 脚本，不带 → final 脚本。

    calls 记录每次请求的 payload，供断言「收尾请求确实不带 tools」。
    """

    def __init__(self, tool_rounds: list, final: list | None = None, status: int = 200):
        self.tool_rounds = list(tool_rounds)
        self.final = final
        self.status = status
        self.tool_round_idx = 0
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        payload = json or {}
        # 深拷贝：循环里的 msgs 是同一个 list，原地 mutate（含上下文裁剪）。
        # 存引用会让「每轮发出去的消息是否合法」这类断言全部失真。
        self.calls.append(copy.deepcopy(payload))
        if "tools" in payload:
            idx = min(self.tool_round_idx, len(self.tool_rounds) - 1)
            chunks = self.tool_rounds[idx] if self.tool_rounds else []
            self.tool_round_idx += 1
        else:
            chunks = self.final or []
        return _FakeStream(_FakeResponse(chunks, self.status))

    @property
    def tool_requests(self) -> list[dict]:
        return [c for c in self.calls if "tools" in c]

    @property
    def finalize_requests(self) -> list[dict]:
        return [c for c in self.calls if "tools" not in c]


def _install(monkeypatch, client: FakeClient, executed: list):
    """装配假模型服务 + 假数据层 + 假工具执行，返回可直接调用 chat_stream 的环境。"""
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
        return {"ok": True, "tool": name}

    monkeypatch.setattr(ai_tools, "execute_tool", fake_execute)


def _run(monkeypatch, client: FakeClient) -> tuple[list[dict], list]:
    executed: list = []
    _install(monkeypatch, client, executed)
    # 注意：用户消息必须在能力闸门的在域语料内（闸门上线后，能力外请求
    # 一轮工具都不会发——那由 test_ai_scope_gate.py 单独锁定）。
    events = list(ai_coach.chat_stream(None, [{"role": "user", "content": "帮我分析下我的恢复状态"}]))
    return events, executed


def _deltas(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


# ---------------------------------------------------------------- 1. 重复调用短路

def test_repeat_tool_call_is_short_circuited(monkeypatch):
    """模型每一轮都重复同一个工具同一参数：只允许真正执行一次。"""
    same = [_tool_chunk("get_athlete_profile")]
    client = FakeClient(tool_rounds=[same, same, same], final=[_text_chunk("好的，我直接回答。")])
    events, executed = _run(monkeypatch, client)

    tools = [e for e in events if e["type"] == "tool"]
    assert len(tools) == 3, f"应有 3 次工具事件，实际 {len(tools)}"
    assert tools[0]["ok"] is True and tools[0]["label"] == "查询跑者档案"
    assert tools[1]["ok"] is False and "跳过重复" in tools[1]["label"]
    assert tools[2]["ok"] is False and "跳过重复" in tools[2]["label"]
    assert executed == [("get_athlete_profile", {})], f"只应真正执行一次，实际 {executed}"


def test_stalled_rounds_stop_early(monkeypatch):
    """连续两轮零新工具即提前收尾，不再把 6 轮全部烧完。"""
    same = [_tool_chunk("get_athlete_profile")]
    client = FakeClient(tool_rounds=[same] * 6, final=[_text_chunk("收尾作答")])
    events, _ = _run(monkeypatch, client)

    assert client.tool_round_idx == 3, f"应在第 3 轮后收尾，实际发了 {client.tool_round_idx} 次工具请求"
    assert client.tool_round_idx < ai_coach.MAX_TOOL_ROUNDS
    assert len(client.finalize_requests) == 1
    assert "收尾作答" in _deltas(events)


# ---------------------------------------------------------------- 2. 触顶优雅降级

def test_exhausted_rounds_still_produces_answer(monkeypatch):
    """复现「同步高驰课表」原场景：6 轮全在扫不同工具 → 仍须给出人话。"""
    scans = [[_tool_chunk(name, call_id=f"call_{i}")] for i, name in enumerate([
        "get_athlete_profile", "get_training_paces", "get_performance_prediction",
        "get_assessment", "get_recovery_status", "get_training_status"])]
    client = FakeClient(
        tool_rounds=scans,
        final=[_text_chunk("我无法直接同步高驰课表，请到「设置 → 平台连接」页手动操作。")])
    events, executed = _run(monkeypatch, client)

    assert len(executed) == 6, "6 个不同工具都应真实执行"
    assert client.tool_round_idx == ai_coach.MAX_TOOL_ROUNDS
    finalize = client.finalize_requests
    assert len(finalize) == 1, "触顶后必须发起恰好一次收尾请求"
    assert "tools" not in finalize[0], "收尾请求必须禁用工具"
    assert "平台连接" in _deltas(events)
    assert not [e for e in events if e["type"] == "error"], "有答复就不该再报错"


# ---------------------------------------------------------------- 3. 收尾失败的最后兜底

def test_finalize_failure_uses_actionable_message(monkeypatch):
    """收尾也产出不了内容时，兜底文案必须可执行，而不是「请换个问法」。"""
    scans = [[_tool_chunk(name, call_id=f"call_{i}")] for i, name in enumerate([
        "get_athlete_profile", "get_training_paces", "get_performance_prediction",
        "get_assessment", "get_recovery_status", "get_training_status"])]
    client = FakeClient(tool_rounds=scans, final=[])   # 收尾返回空
    events, _ = _run(monkeypatch, client)

    assert len(client.finalize_requests) == 1, "必须真的尝试过收尾，而不是直接报错"
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors, "收尾失败应有 error 事件"
    assert errors[0] == ai_coach.FINALIZE_FAILED_MSG
    assert "请换个问法" not in errors[0], "不得再把系统性能力缺口说成用户的提问姿势问题"
    assert "平台连接" in errors[0]


# ---------------------------------------------------------------- 4. 正常路径不受影响

def test_normal_tool_then_answer_skips_finalize(monkeypatch):
    """一轮工具 + 一轮文本的常规问答：不得触发收尾请求。"""
    client = FakeClient(
        tool_rounds=[[_tool_chunk("get_recovery_status")], [_text_chunk("你恢复得不错。")]],
        final=[_text_chunk("不该被调用")])
    events, executed = _run(monkeypatch, client)

    assert executed == [("get_recovery_status", {})]
    assert client.tool_round_idx == 2
    assert client.finalize_requests == [], "正常结束不应发起收尾请求"
    assert _deltas(events) == "你恢复得不错。"
    assert not [e for e in events if e["type"] == "error"]


def test_model_http_error_is_reported(monkeypatch):
    """模型服务返回非 200：仍走 error 且不误发收尾请求。"""
    client = FakeClient(tool_rounds=[[]], final=[], status=500)
    events, _ = _run(monkeypatch, client)

    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors and "模型服务返回 500" in errors[0]
    assert client.finalize_requests == []


# ---------------------------------------------------------------- 5. 提示词能力边界

def test_system_prompt_declares_capability_boundary():
    """提示词必须显式声明能力边界与 UI 指路，否则小模型只会硬扫工具。"""
    sp = ai_coach.SYSTEM_PROMPT
    assert "能力边界" in sp
    assert "不能发起数据同步" in sp
    assert "设置 → 平台连接" in sp
    assert "不支持从手表反向导入课表" in sp
    assert "严禁为了「显得做了点什么」而调用与用户问题无关的工具" in sp


def test_finalize_instruction_forbids_tool_requests():
    """收尾指令必须明确禁止再请求工具，并要求承认做不到 + 指路。"""
    ins = ai_coach.FINALIZE_INSTRUCTION
    assert "不要再要求调用任何工具" in ins
    assert "如实说明你做不到" in ins


# ---------------------------------------------------------------- 6. 上下文预算与裁剪不变量
#
# 2026-09-13 实测（Qwen3-8B + llama-server，读 /v1/chat/completions 的 usage）：
#   仅系统提示词              → prompt_tokens = 1376
#   系统提示词 + 33 个工具     → prompt_tokens = 6417（工具 schema 占 5041）
# 而 llama-server 的 n_ctx_slot = 8192：静态部分吃掉约 78%，动态内容只剩不到 1.8K。
# 旧预算写的是 22000 字符（≈ 1.3 万 token），远大于窗口——等于没设防，
# 超出的部分由 llama-server 静默丢弃 prompt 头部（工具定义 + 系统提示词），
# 提示词里的能力边界护栏会被一并冲掉。下面四条锁住修复后的行为。

_MEASURED_TOOLS_TOKENS = 5041
# 恢复重建基线：原实测 1376 对应的是旧版提示词；现提示词加入了能力闸门指引等
# 段落，llama-server 侧的实测值要等服务可用后重测，先按估算器口径重新锚定，
# 保证「估算 vs 实测」的带宽校验仍能拦住 4 字符 1 token 这类粗暴折算回归。
_MEASURED_SYSTEM_TOKENS = 1450


def _deep_msgs(turns: int, tool_chars: int = 400) -> list[dict]:
    """构造与真实循环同形的消息：一条用户问题 + N 轮工具调用（结果都很长）。

    真实链路只会在末尾追加 assistant(tool_calls)/tool，不会反复追加 user，
    所以这里必须同形，否则测的是线上不存在的形状。
    """
    msgs: list[dict] = [{"role": "system", "content": "SYS"},
                        {"role": "user", "content": "问题" + "。" * 99}]
    for t in range(turns):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{t}", "type": "function",
             "function": {"name": "get_training_status", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{t}", "content": "结" * tool_chars})
    return msgs


def _assert_pairs_intact(msgs: list[dict]) -> None:
    """不变量：每个 tool 消息都有前置 assistant 携带同 id 的 tool_calls。"""
    ids_seen: set[str] = set()
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            ids_seen |= {tc["id"] for tc in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in ids_seen, \
                f"第 {i} 条 tool 消息没有前置 tool_calls 配对（非法序列，服务端会 400）"


def test_token_estimator_is_calibrated_to_measurement():
    """估算器必须贴近实测值，且方向必须是「高估」——低估会让预算形同虚设。

    上限放到 1.45 是刻意的：估算器对 ASCII 占比高的文本保守高估（系统提示词实测 +30%），
    这是设计取舍而非缺陷。真正要拦住的是「改用 4 字符 1 token 的粗暴折算」这类回归
    （那样 33 个工具的 schema 会被估成 ~2950，跌破下限）。
    """
    schema = _dumps(ai_tools.TOOLS_SCHEMA)
    est = ai_coach._est_tokens(schema)
    assert _MEASURED_TOOLS_TOKENS * 0.95 <= est <= _MEASURED_TOOLS_TOKENS * 1.45, est
    sys_est = ai_coach._est_tokens(ai_coach.SYSTEM_PROMPT)
    assert _MEASURED_SYSTEM_TOKENS * 0.95 <= sys_est <= _MEASURED_SYSTEM_TOKENS * 1.45, sys_est
    # 中文不得按「4 字符 1 token」折算（那会低估近一半）
    assert ai_coach._est_tokens("中" * 400) > 200


def test_trim_drops_oldest_segments_keeps_pairs_and_question(monkeypatch):
    """裁剪从最旧的段开始丢：不产生孤立 tool 消息，当前问题与最新结果一字不动。

    预算按新契约由调用方按本轮实际下发的 schema 推（_active_budget），这里直接传。
    """
    msgs = _deep_msgs(6)

    dropped = ai_coach._trim_context(msgs, 600)

    assert dropped >= 1, "超预算必须真的裁掉历史"
    # 先断不变量：它才是这个函数的契约，报错应当直指「孤立 tool 消息」
    _assert_pairs_intact(msgs)
    assert ai_coach._msgs_tokens(msgs) <= 600
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"], \
        "应只剩 system + 当前问题 + 最新一段"
    assert msgs[-1]["tool_call_id"] == "c5", "最新一段必须保留"


def test_trim_does_not_mangle_when_nothing_left_to_drop(monkeypatch):
    """已无可裁段时：不丢消息、不截断内容，只把根因暴露成告警。"""
    msgs = _deep_msgs(1, tool_chars=800)
    before = len(msgs[-1]["content"])

    dropped = ai_coach._trim_context(msgs, 300)

    assert dropped == 0
    assert len(msgs) == 4, "只剩一段时不得再丢消息"
    assert len(msgs[-1]["content"]) == before, "不得用截断工具结果来假装配平预算"
    _assert_pairs_intact(msgs)


def test_ctx_overflow_warning_is_throttled(monkeypatch):
    """超预算告警只响亮一次（warning），之后降级——工具循环每轮都撞它，不能刷屏。"""
    monkeypatch.setattr(ai_coach, "_ctx_overflow_warned", False)
    seen: list[str] = []
    monkeypatch.setattr(ai_coach, "logger", SimpleNamespace(
        warning=lambda msg, *a: seen.append("warning:" + (msg % a)),
        info=lambda msg, *a: seen.append("info:" + (msg % a))))

    ai_coach._warn_ctx_overflow(9999, 500, 300)
    ai_coach._warn_ctx_overflow(9999, 500, 300)

    assert len(seen) == 2
    assert seen[0].startswith("warning:"), "首次必须足够响亮"
    assert seen[1].startswith("info:"), "后续不得继续刷 warning"
    assert "ai_context_tokens" in seen[0], "告警必须给出可执行的下一步"


def test_tool_loop_never_sends_illegal_message_sequence(monkeypatch):
    """预算压到极小跑完整条循环：每轮真正发出去的 messages 都必须合法且不丢系统提示词。

    这是对「上下文压力下服务端 400 / 能力边界护栏被冲掉」的端到端回归。
    """
    # 预算决策按新契约来自 _active_budget(schema_tokens)：压到 500 模拟极小窗口
    monkeypatch.setattr(ai_coach, "_active_budget", lambda tools_tokens: 500)
    scans = [[_tool_chunk(name, call_id=f"call_{i}")] for i, name in enumerate([
        "get_athlete_profile", "get_training_paces", "get_performance_prediction",
        "get_assessment", "get_recovery_status", "get_training_status"])]
    client = FakeClient(tool_rounds=scans, final=[_text_chunk("收尾作答")])
    executed: list = []
    _install(monkeypatch, client, executed)
    history: list[dict] = []
    for i in range(4):
        history.append({"role": "user", "content": f"第{i}个问题" + "。" * 200})
        history.append({"role": "assistant", "content": "回答" + "。" * 200})

    events = list(ai_coach.chat_stream(None, history))

    assert len(client.tool_requests) >= 2, "至少要发多轮才谈得上上下文压力"
    for payload in client.calls:
        assert payload["messages"][0]["role"] == "system", "任何一轮都不得丢掉系统提示词"
        _assert_pairs_intact(payload["messages"])
    # 第 1 轮发出时还没裁剪过；此后每轮都应已被压回预算内
    for payload in client.tool_requests[1:]:
        assert ai_coach._msgs_tokens(payload["messages"]) <= 500
    assert _deltas(events).endswith("收尾作答")

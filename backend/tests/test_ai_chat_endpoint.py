"""对话端点的输入契约：会话模式下送给模型的 history 必须包含「本轮用户消息」。

背景（2026-09-13 决定性实证，用内存库 + TestClient 复现）：
`routers/ai.py` 原先在写入当前用户消息**之前**就构建了 history ——

    rows = db.scalars(...库内已有消息...).all()      # ← 本轮消息还没落库
    history = [{role, content} for m in rows]
    db.add(AiMessage(role="user", content=user_text))  # ← 才落库

于是：新会话首条消息 → ``history = []``，模型只拿到系统提示词；
其后每一轮 → history 只到上一轮，模型永远慢一拍。
而提示词铁律 1 要求「涉及个人数据必须先调用工具」，模型在
「手里有 33 个工具、却没问题可答」的状态下只能按 schema 顺序扫零参数工具 ——
这正是复盘里那 6 次无关调用与「已达最大工具轮数（6）」报错的成因。

本文件锁定修复后的事实：本轮用户消息必须出现在送给模型的 history 末尾。
"""
from __future__ import annotations

import pytest
from app.db import Base
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient


@pytest.fixture()
def mem_db():
    """独立内存库（StaticPool 跨线程共享），不碰真实数据库文件。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def captured(monkeypatch):
    """把 chat_stream 换成一个只记录入参的假实现，返回被记录的历史列表。"""
    from app.services import ai_coach

    seen: list[list[dict]] = []

    def fake_chat_stream(db, history):
        seen.append([dict(m) for m in history])
        yield {"type": "delta", "text": "（假回答）"}

    monkeypatch.setattr(ai_coach, "chat_stream", fake_chat_stream)
    return seen


@pytest.fixture()
def api(mem_db, captured):
    """覆盖 get_db 依赖的 TestClient；库内建好默认跑者档案。"""
    from app.db import get_db
    from app.main import app

    mem_db.add(make_athlete(name="测试跑者", birth_year=1995,
                            height_cm=175.0, weight_kg=65.0,
                            resting_hr=55, max_hr=190))
    mem_db.commit()

    def _ov():
        yield mem_db

    app.dependency_overrides[get_db] = _ov
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _new_conv(client) -> int:
    r = client.post("/api/ai/conversations", json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ask(client, cid: int, text: str) -> None:
    r = client.post("/api/ai/chat", json={"conversation_id": cid, "message": text})
    assert r.status_code == 200, r.text


def test_first_message_reaches_the_model(api, captured):
    """新会话首条消息：模型必须看到这个问题，而不是空历史。"""
    cid = _new_conv(api)
    _ask(api, cid, "请你去同步一下我在高驰上的课表")

    assert captured, "chat_stream 必须被调用"
    history = captured[-1]
    assert history == [{"role": "user", "content": "请你去同步一下我在高驰上的课表"}], \
        f"首条消息必须原样进入 history，实际 {history}"


def test_current_turn_is_last_and_previous_turns_kept(api, captured):
    """多轮：末尾是本轮问题，且上一轮问答按序保留（不能少一轮、不能有重复）。"""
    cid = _new_conv(api)
    _ask(api, cid, "我最近状态怎么样")
    _ask(api, cid, "那把我周二的课挪到周四")

    history = captured[-1]
    assert history[-1] == {"role": "user", "content": "那把我周二的课挪到周四"}, \
        f"末尾必须是本轮问题，实际 {history[-1]}"
    assert [m["role"] for m in history] == ["user", "assistant", "user"], \
        f"角色序列应为 用户/助手/用户，实际 {[m['role'] for m in history]}"
    assert history[0]["content"] == "我最近状态怎么样"
    assert "请你去同步" not in str(history), "不得串入其它会话的内容"


def test_three_turns_never_lags_behind(api, captured):
    """连续三轮：每一轮送出的 history 末尾都必须等于该轮刚输入的那句话。

    这是对「每轮都慢一拍」这一原始缺陷最直接的回归断言。
    """
    cid = _new_conv(api)
    questions = ["第一句", "第二句", "第三句"]
    for q in questions:
        _ask(api, cid, q)
        assert captured[-1][-1]["content"] == q, \
            f"第 {questions.index(q) + 1} 轮 history 末尾应为 {q!r}，实际 {captured[-1][-1]}"
    assert len(captured) == 3


def test_stateless_mode_passes_client_messages_through(api, captured):
    """无 conversation_id 的旧契约不受影响：history 就是客户端发来的 messages。"""
    payload = [{"role": "user", "content": "帮我看看这周的课表"},
               {"role": "assistant", "content": "好的"},
               {"role": "user", "content": "那周四那节是什么强度"}]
    r = api.post("/api/ai/chat", json={"messages": payload})
    assert r.status_code == 200, r.text
    assert captured[-1] == payload, "无状态模式必须原样透传，不得被服务端改写"

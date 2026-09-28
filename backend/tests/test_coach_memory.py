"""教练长期记忆（coach_notes / remember_user_note）与滚动摘要（上下文压缩）的回归。

长期记忆：对话中 remember_user_note 工具沉淀持久事实，每轮注入 system prompt，
用户可增删（/api/ai/notes）。滚动摘要：历史超预算时被裁掉的旧段压缩成要点
存 ai_conversations.summary 并注入下一轮头部——「截断即遗忘」变「截断前先记要点」。
"""
from __future__ import annotations

import pytest
from app import models
from app.services import ai_coach, ai_tools
from factories import make_athlete
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    a = make_athlete()
    s.add(a)
    s.commit()
    yield s
    s.close()


def _athlete(db) -> models.Athlete:
    return db.query(models.Athlete).first()


# ---------------------------------------------------------------- 长期记忆

def test_remember_note_writes_and_dedupes(db):
    a = _athlete(db)
    r1 = ai_tools.remember_note(db, a.id, "右膝有旧伤，深蹲会疼", "injury")
    assert r1["ok"] is True and not r1.get("duplicate")
    r2 = ai_tools.remember_note(db, a.id, "右膝有旧伤，深蹲会疼", "injury")
    assert r2["ok"] is True and r2["duplicate"] is True, "重复内容应幂等"
    assert len(db.scalars(ai_tools.select(models.CoachNote).where(
        models.CoachNote.athlete_id == a.id)).all()) == 1


def test_remember_note_validates_input(db):
    a = _athlete(db)
    assert ai_tools.remember_note(db, a.id, "短")["ok"] is False
    assert ai_tools.remember_note(db, a.id, "")["ok"] is False
    # 未知类别归入 other 而不是报错（模型偶发编 enum 值）
    r = ai_tools.remember_note(db, a.id, "喜欢晨跑", "未知类别")
    assert r["ok"] is True
    note = db.query(models.CoachNote).filter_by(athlete_id=a.id).one()
    assert note.category == "other"


def test_remember_note_cap_deactivates_oldest(db, monkeypatch):
    a = _athlete(db)
    monkeypatch.setattr(ai_tools, "NOTE_MAX_ACTIVE", 3)
    for i in range(5):
        assert ai_tools.remember_note(db, a.id, f"记忆条目 {i}")["ok"] is True
    active = db.scalars(ai_tools.select(models.CoachNote).where(
        models.CoachNote.athlete_id == a.id,
        models.CoachNote.active.is_(True)).order_by(models.CoachNote.id)).all()
    assert [n.content for n in active] == ["记忆条目 2", "记忆条目 3", "记忆条目 4"], \
        "超上限时应停用最旧的，保留最新 3 条"


def test_remember_user_note_tool_registered_and_resident():
    assert "remember_user_note" in ai_tools.TOOL_IMPLS
    assert {t["function"]["name"] for t in ai_tools.TOOLS_SCHEMA} >= {"remember_user_note"}
    assert "remember_user_note" in ai_tools.TOOL_LABELS
    from app.services import ai_coach
    assert "remember_user_note" not in ai_coach._OPTIONAL_TOOLS, "记忆工具必须常驻下发"


def test_system_prompt_injects_notes(db):
    a = _athlete(db)
    ai_tools.remember_note(db, a.id, "右膝有旧伤", "injury")
    ai_tools.remember_note(db, a.id, "每周三加班没法夜跑", "life")
    prompt = ai_coach.system_prompt(db)
    assert "【已记录的长期记忆】" in prompt
    assert "[伤病] 右膝有旧伤" in prompt
    assert "[生活] 每周三加班没法夜跑" in prompt


def test_system_prompt_without_notes_has_no_empty_section(db):
    prompt = ai_coach.system_prompt(db)
    assert "【已记录的长期记忆】" not in prompt, "空记忆不应在提示词里留空章节"


# ---------------------------------------------------------------- 滚动摘要（上下文压缩）

def _long_msgs(n_turns: int = 6, fill: int = 800) -> list[dict]:
    """构造超预算的多轮历史：[system, (user, assistant) × n, 当前问题]。"""
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"历史问题 {i}：" + "跑" * fill})
        msgs.append({"role": "assistant", "content": f"历史回答 {i}：" + "步" * fill})
    msgs.append({"role": "user", "content": "当前问题：这周练什么"})
    return msgs


def test_compress_and_trim_injects_summary_and_persists(monkeypatch):
    monkeypatch.setattr(ai_coach, "summarize_dialogue",
                        lambda dialog, old_summary="": f"摘要[{old_summary or '无'}]")
    msgs = _long_msgs()
    persisted: list[str] = []
    ai_coach._compress_and_trim(msgs, budget=1500, summary=None,
                                on_summary=persisted.append,
                                client=None, base="http://127.0.0.1:1/v1", model_id="m")
    assert persisted == ["摘要[无]"], "新摘要必须经回调持久化"
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"].startswith(ai_coach.SUMMARY_HEADER)
    assert "摘要[无]" in msgs[1]["content"]
    assert msgs[-1]["content"] == "当前问题：这周练什么", "当前问题绝不能被裁"
    assert ai_coach._msgs_tokens(msgs) <= 1500, "注入摘要后总量仍在预算内"


def test_compress_and_trim_merges_old_summary(monkeypatch):
    monkeypatch.setattr(ai_coach, "summarize_dialogue",
                        lambda dialog, old_summary="": f"合并自[{old_summary}]")
    msgs = [m for m in _long_msgs()]
    msgs.insert(1, {"role": "system", "content": ai_coach.SUMMARY_HEADER + "旧摘要要点"})
    persisted: list[str] = []
    ai_coach._compress_and_trim(msgs, budget=1500, summary="旧摘要要点",
                                on_summary=persisted.append,
                                client=None, base="http://127.0.0.1:1/v1", model_id="m")
    assert persisted == ["合并自[旧摘要要点]"], "旧摘要应传给模型做合并"
    # 原地替换而不是叠加：system 注记仍只有一个
    assert [m for m in msgs if m["role"] == "system"] == [msgs[0], msgs[1]]


def test_compress_and_trim_falls_back_to_plain_trim(monkeypatch):
    monkeypatch.setattr(ai_coach, "summarize_dialogue", lambda *a, **k: None)
    msgs = _long_msgs()
    persisted: list[str] = []
    ai_coach._compress_and_trim(msgs, budget=1500, summary=None,
                                on_summary=persisted.append,
                                client=None, base="http://127.0.0.1:1/v1", model_id="m")
    assert persisted == [], "摘要失败不落库"
    assert not [m for m in msgs if m["role"] == "system" and m["content"].startswith(ai_coach.SUMMARY_HEADER)]
    assert msgs[-1]["content"] == "当前问题：这周练什么"
    assert ai_coach._msgs_tokens(msgs) <= 1500, "退化为纯裁剪时仍压回预算"


def test_compress_and_trim_noop_when_under_budget():
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "当前问题：这周练什么"}]
    assert ai_coach._compress_and_trim(msgs, budget=100000, summary=None,
                                       on_summary=lambda s: None,
                                       client=None, base="b", model_id="m") == 0


def test_trim_context_collects_dropped_pairs():
    """dropped_out 必须收走被裁的整段（assistant 与它的 tool 结果不拆散）。"""
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "旧问题：" + "跑" * 900},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_daily_checkin", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "结果" + "数" * 900},
            {"role": "assistant", "content": "旧回答：" + "步" * 900},
            {"role": "user", "content": "当前问题"}]
    dropped: list[dict] = []
    ai_coach._trim_context(msgs, budget=1200, dropped_out=dropped)
    assert dropped, "应有段被裁掉"
    assert [m["role"] for m in dropped].count("tool") in (0, 1)
    if "tool" in [m["role"] for m in dropped]:
        # tool 消息若被裁，携带它的 assistant(tool_calls) 必须在同一段里
        idx_tool = [m["role"] for m in dropped].index("tool")
        assert dropped[idx_tool - 1]["role"] == "assistant" and dropped[idx_tool - 1].get("tool_calls")

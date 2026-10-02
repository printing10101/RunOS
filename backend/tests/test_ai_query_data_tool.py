"""AI 教练工具 query_data（跨记录聚合统计）：注册、白名单错误自纠错与数字正确性。

背景：独立数据探索页已下线，探索能力归 AI 教练——query_data 复用
services/explore.py 的白名单引擎（test_explore_endpoint.py 经 HTTP 端点
锁定同一引擎的红线）。这里锁定工具侧：维度值中文标签、错误信息带可用
选项（LLM 拿到即可自纠错重试）、schema/实现/常驻三处注册同步。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from app import models
from app.services import ai_coach, ai_tools, explore
from app.services.ai_coach import _OPTIONAL_TOOLS
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


def _seed_runs(db):
    """默认档案三条活动：两跑一骑（2026-09-01 周二 / 09-15 周二 / 09-20 周日）。"""
    a = db.query(models.Athlete).first()
    db.add_all([
        models.Activity(athlete_id=a.id, sport="run", title="轻松跑", platform="coros",
                        start_time=datetime(2026, 9, 1, 7, 0), duration_sec=1800,
                        distance_m=5000, training_load=100),
        models.Activity(athlete_id=a.id, sport="run", title="长距离", platform="coros",
                        start_time=datetime(2026, 9, 15, 8, 0), duration_sec=3600,
                        distance_m=10000, training_load=200),
        models.Activity(athlete_id=a.id, sport="ride", title="骑行", platform="manual",
                        start_time=datetime(2026, 9, 20, 9, 0), duration_sec=3600,
                        distance_m=20000, training_load=150),
    ])
    db.commit()


# ---------------------------------------------------------------- 注册同步
def test_query_data_registered_and_resident():
    """schema / 实现 / 常驻集三处必须同步——漏一处模型就是拿到未知工具空转。"""
    names = {t["function"]["name"] for t in ai_tools.TOOLS_SCHEMA}
    assert "query_data" in names
    assert "query_data" in ai_tools.TOOL_IMPLS
    assert "query_data" not in _OPTIONAL_TOOLS  # 查询类默认常驻，永远下发


def test_tool_description_lists_all_tables():
    """说明从注册表动态生成：每张登记表都必须出现在工具描述里。"""
    desc = next(t for t in ai_tools.TOOLS_SCHEMA
                if t["function"]["name"] == "query_data")["function"]["description"]
    for key, t in explore.TABLES.items():
        assert f"{key}({t['label']})" in desc


# ---------------------------------------------------------------- 数字与标签
def test_group_by_sport_returns_labeled_rows(db):
    _seed_runs(db)
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "group_by": ["sport"],
        "metrics": [{"col": "*", "agg": "count"}, {"col": "distance_m", "agg": "sum"}],
    })
    assert r["ok"] is True, r.get("reasons")
    assert r["total"] == 2
    # 首指标（记录数）降序：跑步在前；维度值已套中文标签
    assert r["rows"][0]["sport"] == "跑步"
    assert r["rows"][0]["*_count"] == 2
    assert r["rows"][0]["distance_m_sum"] == 15000
    assert r["rows"][1]["sport"] == "骑行"
    assert r["rows"][1]["distance_m_sum"] == 20000
    assert "label_maps" not in r  # 已套进行值，不再回传


def test_weekday_grouping_labeled_chinese(db):
    _seed_runs(db)
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "group_by": ["weekday"],
        "metrics": [{"col": "*", "agg": "count"}],
    })
    by_day = {row["weekday"]: row["*_count"] for row in r["rows"]}
    assert by_day == {"周二": 2, "周日": 1}


def test_date_range_narrows_window(db):
    _seed_runs(db)
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "metrics": [{"col": "*", "agg": "count"}],
        "date_from": "2026-09-16", "date_to": "2026-09-20",
    })
    assert r["rows"][0]["*_count"] == 1


# ---------------------------------------------------------------- 错误自纠错
def test_unknown_table_error_lists_options(db):
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "runs", "metrics": [{"col": "*", "agg": "count"}]})
    assert r["ok"] is False
    assert "可用" in r["reasons"][0] and "activities" in r["reasons"][0]


def test_unknown_metric_error_lists_measures(db):
    _seed_runs(db)
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "metrics": [{"col": "pace_sec", "agg": "avg"}]})
    assert r["ok"] is False
    assert "可用指标" in r["reasons"][0] and "distance_m" in r["reasons"][0]


def test_star_only_pairs_with_count(db):
    """\"*\" 配 sum 属非法组合：错误信息要能引导模型改成 count 或换指标。"""
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "metrics": [{"col": "*", "agg": "sum"}]})
    assert r["ok"] is False
    assert "只配 count" in r["reasons"][0]


def test_malformed_args_brief_reason(db):
    """agg 词汇外溢出 pydantic 校验：报一句话，不抛 500 也不静默。"""
    a = db.query(models.Athlete).first()
    r = ai_tools.execute_tool(db, a, "query_data", {
        "table": "activities", "metrics": [{"col": "distance_m", "agg": "median"}]})
    assert r["ok"] is False
    assert "参数不合法" in r["reasons"][0]


# ---------------------------------------------------------------- 工具循环联调（假模型 + 真工具 + 真引擎）

class _Resp:
    status_code = 200

    def __init__(self, chunks: list[dict]):
        self._lines = ["data: " + json.dumps(c, ensure_ascii=False) for c in chunks]
        self._lines.append("data: [DONE]")

    def iter_lines(self):
        yield from self._lines


class _Stream:
    def __init__(self, resp: _Resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class _ScriptedClient:
    """带 tools 的请求按序回放 tool_rounds，否则回放 final；calls 留全量 payload 供断言。"""

    def __init__(self, tool_rounds: list[dict], final: list[dict]):
        self._tool_rounds = tool_rounds
        self._final = final
        self._idx = 0
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        payload = json or {}
        self.calls.append(payload)
        if "tools" in payload:
            chunks = self._tool_rounds[min(self._idx, len(self._tool_rounds) - 1)]
            self._idx += 1
        else:
            chunks = self._final
        return _Stream(_Resp(chunks))


def _install_fake_llm(monkeypatch, client: _ScriptedClient) -> None:
    real_httpx = ai_coach.httpx
    monkeypatch.setattr(ai_coach, "probe", lambda: {
        "reachable": True, "model": "mock", "base_url": "http://127.0.0.1:1/v1",
        "models": ["mock"], "model_ready": True})
    monkeypatch.setattr(ai_coach, "system_prompt", lambda db: "SYS")
    monkeypatch.setattr(ai_coach, "httpx", SimpleNamespace(
        Client=lambda **kw: client,
        Timeout=real_httpx.Timeout,
        ConnectError=real_httpx.ConnectError,
        ReadTimeout=real_httpx.ReadTimeout,
        RemoteProtocolError=real_httpx.RemoteProtocolError))


def test_query_data_flows_through_chat_loop(db, monkeypatch):
    """端到端锁定管线（不是模型智商）：schema 下发 → 工具分发 → 真引擎执行 →
    标签化结果回灌下一轮 → 事件流与最终答案。"""
    _seed_runs(db)
    tool_call = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "type": "function",
         "function": {"name": "query_data",
                      "arguments": json.dumps(
                          {"table": "activities", "group_by": ["sport"],
                           "metrics": [{"col": "*", "agg": "count"},
                                       {"col": "distance_m", "agg": "sum"}]},
                          ensure_ascii=False)}}]}}]}
    client = _ScriptedClient(
        tool_rounds=[[tool_call]],
        final=[{"choices": [{"delta": {"content": "跑步 2 次共 15 公里，骑行 1 次共 20 公里。"}}]}])
    _install_fake_llm(monkeypatch, client)

    # 问句带「？」确保绕过直通层（fastpath 只答短句白名单），走完整工具循环
    events = list(ai_coach.chat_stream(
        db, [{"role": "user", "content": "帮我统计下跑步和骑行各有多少次？"}]))

    # 首轮请求的 tools 里有 query_data，描述由探索注册表动态生成
    first_tools = {t["function"]["name"] for t in client.calls[0]["tools"]}
    assert "query_data" in first_tools
    desc = next(t for t in client.calls[0]["tools"]
                if t["function"]["name"] == "query_data")["function"]["description"]
    assert "activities(训练活动)" in desc
    # 下一轮请求回灌的工具结果：维度值已套中文标签、数字来自真引擎
    tool_msg = next(m for m in client.calls[1]["messages"] if m["role"] == "tool")
    assert "跑步" in tool_msg["content"] and "distance_m_sum" in tool_msg["content"]
    assert "15000" in tool_msg["content"]
    # 事件流：工具事件带中文标签且 ok=True，最终答案原样流出
    tool_event = next(e for e in events if e["type"] == "tool" and e["name"] == "query_data")
    assert tool_event["ok"] is True and tool_event["label"] == "跨记录统计查询"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == \
        "跑步 2 次共 15 公里，骑行 1 次共 20 公里。"

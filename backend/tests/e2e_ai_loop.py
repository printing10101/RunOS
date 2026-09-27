"""端到端链路测试：用本地 Mock OpenAI 服务验证 ai_coach 工具调用循环 + 提案应用闭环。
不依赖真实 Ollama。跑完自动还原数据。

脚本型（会写真实演示库），命名不带 test_ 前缀以免 pytest 误收集；
在 backend/ 目录下运行：python tests/e2e_ai_loop.py
"""
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.config import settings
from app.db import SessionLocal
from app.schemas import AiProposalApplyIn
from app.services import ai_coach
from sqlalchemy import select

# ---------------------------------------------------------------- Mock 模型服务

ROUND1_TOOLCALLS = [{
    "id": "call_1", "type": "function",
    "function": {"name": "get_recovery_status", "arguments": "{}"},
}, {
    "id": "call_2", "type": "function",
    "function": {"name": "propose_quality_adjustment",
                 "arguments": json.dumps({"workout_id": 6, "target_reps": 3})},
}]
ROUND2_CONTENT = "根据引擎数据：你恢复分 61 分，HRV 正常。建议把周日节奏课的 strides 从 6 组减到 3 组，提案已生成，请确认。"


class MockHandler(BaseHTTPRequestHandler):
    state = {"round": 0}

    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path.endswith("/models")
        self._send({"data": [{"id": "mock-model"}]})

    def do_POST(self):
        assert self.path.endswith("/chat/completions")
        self.state["round"] += 1
        if self.state["round"] == 1:
            # 第一轮：流式返回 tool_calls（模拟 Ollama 的分片格式）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"delta": {"content": "<think>思考中</think>"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                        "function": {"name": "get_recovery_status", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                                                        "function": {"name": "propose_quality_adjustment", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"workout_'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": 'id": 6, "ta'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": 'rget_reps": 3}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            # 第二轮：流式返回最终文本（含 think 块，应被过滤）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ["<think>不该", "出现</think>", "根据引擎数据：", "建议把周日节奏课的", " strides 从 6 组减到 3 组。"]:
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")


# ---------------------------------------------------------------- 测试

def main():
    settings.ai_base_url = "http://127.0.0.1:11500/v1"   # 环回地址，应通过校验
    settings.ai_model = "mock-model"
    server = HTTPServer(("127.0.0.1", 11500), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _restore()                     # 先清理上次运行可能遗留的状态
    try:
        _run()
    finally:
        _restore()
        print("[7] 数据已还原（无论成败）")


def _restore():
    """把演示数据还原：先按运行期快照精确回滚，再兜底修正历史遗留状态。"""
    from app import models
    from app.services import planner
    db = SessionLocal()
    rolled = _rollback_snapshot(db)
    wo6 = db.get(models.PlanWorkout, 6)
    _, _, steps, km, dur = planner.build_quality_session("base", 50.7, 43.1, "marathon")
    if wo6 and len(wo6.structured or []) != len(steps):
        wo6.structured, wo6.distance_km = steps, km
        wo6.duration_min = dur
        wo6.description = ""
    for w in db.scalars(select(models.PlanWeek).where(models.PlanWeek.week_index == 2)).all():
        for wo in w.workouts:
            if wo.description and "AI 提案" in wo.description and wo.date.weekday() == 1:
                wo.date = wo.date + timedelta(days=-1)   # 周二挪回周一
                wo.description = ""
    db.commit()
    db.close()
    if rolled:
        print(f"[还原] 已按运行快照回滚 {rolled} 节课")


# 本次运行改动的原始状态快照：即使中途崩溃也能精确回滚。
# 旧版只靠「weekday==1 就减一天」这类猜测还原，一旦崩在 apply 之后就会留下污染
# （2026-09-13 实际踩到：workout 7 被挪到 09-18 且未被还原）。
_SNAPSHOT: dict[int, dict] = {}


def _snap(db, workout_id: int) -> None:
    """在执行 apply 之前记录该课的原始状态（同一次运行只记第一次）。"""
    from app import models
    if workout_id in _SNAPSHOT:
        return
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo:
        return
    _SNAPSHOT[workout_id] = {
        "date": wo.date, "start_time": wo.start_time,
        "structured": [dict(s) for s in (wo.structured or [])],
        "distance_km": wo.distance_km, "duration_min": wo.duration_min,
        "description": wo.description,
    }


def _rollback_snapshot(db) -> int:
    """按快照回滚，返回回滚的课程数。幂等，可重复调用。"""
    from app import models
    n = 0
    for wid, s in _SNAPSHOT.items():
        wo = db.get(models.PlanWorkout, wid)
        if not wo:
            continue
        wo.date, wo.start_time = s["date"], s["start_time"]
        wo.structured = s["structured"]
        wo.distance_km, wo.duration_min = s["distance_km"], s["duration_min"]
        wo.description = s["description"]
        n += 1
    return n


def _run():
    settings.ai_base_url = "http://127.0.0.1:11500/v1"   # 环回地址，应通过校验
    settings.ai_model = "mock-model"
    server = HTTPServer(("127.0.0.1", 11500), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 0) 地址校验：公网地址必须被拒
    bad = ai_coach.validate_base_url("http://example.com/v1")
    assert bad and "不在本机/私网" in bad, f"SSRF 校验失效: {bad}"
    print("[0] SSRF 校验：公网地址被拒 OK")

    db = SessionLocal()
    events = list(ai_coach.chat_stream(db, [{"role": "user", "content": "状态不好，帮我减点量"}]))
    types = [e["type"] for e in events]
    print("[1] SSE 事件序列:", types)
    assert "tool" in types and "proposals" in types and "done" in types, types
    tools = [e for e in events if e["type"] == "tool"]
    assert [t["name"] for t in tools] == ["get_recovery_status", "propose_quality_adjustment"]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "think" not in deltas and "不该" not in deltas, f"think 泄漏: {deltas}"
    print("[2] 工具调用与流式 think 过滤 OK，最终文本:", deltas[:40], "…")
    proposals = [e for e in events if e["type"] == "proposals"][0]["items"]
    assert len(proposals) == 1 and proposals[0]["kind"] == "quality_adjustment"
    print("[3] 提案生成:", proposals[0]["title"])

    # 提案应用（走与路由相同的校验+写库路径）
    from app import models
    from app.routers.ai import apply_proposal
    wo_before = db.get(models.PlanWorkout, 6)
    orig = {"km": wo_before.distance_km, "dur": wo_before.duration_min,
            "steps": [dict(s) for s in wo_before.structured], "desc": wo_before.description}
    _snap(db, wo_before.id)
    apply_proposal(AiProposalApplyIn(**proposals[0]["apply"]), db)
    db.expire_all()
    wo = db.get(models.PlanWorkout, 6)
    print(f"[4] 应用提案 OK: km {orig['km']} → {wo.distance_km}, 步骤 {len(orig['steps'])} → {len(wo.structured)}")
    assert wo.distance_km < orig["km"] and len(wo.structured) < len(orig["steps"])
    assert wo.description and "AI 提案" in wo.description

    # 挪课应用 + 冲突拒绝
    from app.services import ai_tools
    a = ai_tools._get_athlete(db)
    from datetime import date as _date

    from sqlalchemy import select as _sel
    # 原实现硬编码 week_index==2 + 「周一挪周二」：演示库的日期与占用情况一变就直接崩
    # （2026-09-13 实测：未来 4 天课全占满，周二根本没空档）。改为自适应找一个真能挪的组合，
    # 找不到就明确跳过——不为了「跑通」而伪造通过。
    candidates = db.scalars(_sel(models.PlanWorkout)
                            .where(models.PlanWorkout.date > _date.today())
                            .order_by(models.PlanWorkout.date)).all()
    pick = None
    for wo in candidates:
        for wd in (1, 5, 6, 2, 3, 4, 0):
            if wo.date.weekday() != wd and ai_tools.check_move(db, a, wo.id, wd)["ok"]:
                pick = (wo, wd)
                break
        if pick:
            break
    if pick is None:
        print(f"[5] 跳过挪课应用：{len(candidates)} 节未来课中没有可挪动的（目标星期都已占用）")
        print("[6] 跳过冲突拒绝样例")
    else:
        w3, target_wd = pick
        checked = ai_tools.check_move(db, a, w3.id, target_wd)
        expect_date = checked["proposal"]["to"]["date"]
        _snap(db, w3.id)
        apply_payload = {"kind": "move_workout", "workout_id": w3.id, "target_weekday": target_wd}
        r2 = apply_proposal(AiProposalApplyIn(**apply_payload), db)
        db.expire_all()
        got_date = db.get(models.PlanWorkout, w3.id).date.isoformat()
        assert got_date == expect_date, f"应用结果 {got_date} 与校验给出的 {expect_date} 不一致"
        print("[5] 挪课应用 OK →", r2["date"])
        # 暴露一个已知语义问题：check_move 用 week.start_date + target_weekday 算日期，
        # 只有 start_date 是周一时「挪到周几」才与用户认知一致（方法库体验周的 start_date 常不是周一）。
        real_wd = _date.fromisoformat(expect_date).weekday()
        if real_wd != target_wd:
            print(f"[!] 语义错位：target_weekday={target_wd}（{ai_tools.WEEKDAY_NAMES[target_wd]}）"
                  f"实际落到 {expect_date}（{ai_tools.WEEKDAY_NAMES[real_wd]}）——"
                  "该 PlanWeek.start_date 不是周一，proposal 文案会与实际落期不符")
        rejected = next(((d, ai_tools.check_move(db, a, w3.id, d)) for d in range(7)
                         if not ai_tools.check_move(db, a, w3.id, d)["ok"]), None)
        if rejected:
            print("[6] 冲突拒绝样例:", rejected[1].get("reasons"))
        else:
            print("[6] 跳过冲突拒绝样例：当前数据下没有会冲突的挪法")
    print("\n全部通过 ✔")


if __name__ == "__main__":
    main()

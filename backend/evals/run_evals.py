"""AI 教练质量回归评测：黄金场景 × 确定性断言，直连本地模型逐场景跑。

用法（在 backend/ 目录下）：
  py evals/run_evals.py                          # 全部场景
  py evals/run_evals.py --category proposal_guard
  py evals/run_evals.py --id verbal_adjustment_becomes_proposal
  py evals/run_evals.py --json eval_report.json  # 结果落盘（gitignore 覆盖不到时自行清理）

什么时候跑：改提示词、换模型/量化、动 ai_coach 工具循环之后。
退出码：有失败 = 1，可直接挂脚本/钩子。模型或档案未就绪时报错退出（exit 2），
不算评测失败——「跑不了」和「跑挂了」必须分开。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/ 入 path

from app.data import get_default_athlete  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.services import ai_coach  # noqa: E402

from evals import runner  # noqa: E402

SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 教练质量回归评测")
    ap.add_argument("--scenarios", default=str(SCENARIOS_PATH))
    ap.add_argument("--category", default=None, help="只跑某类（tool_discipline/scope_gate/proposal_guard/number_discipline）")
    ap.add_argument("--id", default=None, help="只跑某个场景")
    ap.add_argument("--json", dest="json_out", default=None, help="结果写入 JSON 文件")
    args = ap.parse_args()

    scenarios = runner.load_scenarios(args.scenarios)
    if args.category:
        scenarios = [s for s in scenarios if s["category"] == args.category]
    if args.id:
        scenarios = [s for s in scenarios if s["id"] == args.id]
    if not scenarios:
        print("没有匹配的场景", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        athlete = get_default_athlete(db)
        if athlete is None:
            print("未创建档案：先完善个人档案再跑评测", file=sys.stderr)
            return 2

        def chat_fn(message: str):
            return ai_coach.chat_stream(db, [{"role": "user", "content": message}])

        results = []
        for sc in scenarios:
            r = runner.run_scenario(chat_fn, sc)
            results.append(r)
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"[{mark}] {r['id']}  tools={r['tools'] or '-'} "
                  f"proposals={r['proposals']} answer={r['answer_chars']}字")
            for f in r["failures"]:
                print(f"       ✗ {f}")

        passed = sum(1 for r in results if r["pass"])
        print(f"\n{passed}/{len(results)} 通过")
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps({"passed": passed, "total": len(results), "results": results},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"报告已写入 {args.json_out}")
        return 0 if passed == len(results) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

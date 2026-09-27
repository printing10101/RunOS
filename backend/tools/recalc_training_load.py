#!/usr/bin/env python
"""历史训练负荷重算：把 activities.training_load 统一到 services/load.py 的口径。

为什么需要
    早期负荷用「时长(min) x 心率 / 140」估算 —— 分母写死 140、完全忽略个体心率，
    且同一个式子散落在 activities.py 与 connections.py 两处。线上同步已改用统一
    引擎，但**历史入库的行仍是旧公式的值**，需要一次性重算。

怎么算
    与线上完全同一条路径：services.load.load_for_activity()，优先级
        平台真实值(raw.coros_training_load) > Banister TRIMP > sRPE > unknown(0)
    因此本脚本**幂等**：重跑一次不应再产生任何改动，可直接用它自检。

用法（在 backend 目录下执行）
    py -3 tools/recalc_training_load.py            # 干跑，只报告（默认）
    py -3 tools/recalc_training_load.py --apply    # 实际写库
    py -3 tools/recalc_training_load.py --limit 5  # 只试跑前 5 条

安全
    - 默认干跑；只有显式 --apply 才写。
    - 只改 training_load 与 raw.training_load_source 两个字段，不动其它列。
    - 写库前请先整库备份（.db 为 gitignore 产物，git 救不回来）。
    - 走 ORM 而非裸 SQL：raw 是 JSON 列，需要 ORM 的序列化与变更追踪。

踩坑记录（已由测试守住）
    首版只把算出的新值放进返回值，没有赋回 `activity.training_load`，
    结果 `commit()` 报告成功、raw 也写进去了，**数值列却静默保持旧值**。
    现在 apply_changes() 会做「写后回读」，落盘不符即返回失败码。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

# 直接以脚本方式运行时 sys.path[0] 是 tools/，需要把 backend/ 补进来
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import Activity, Athlete  # noqa: E402
from app.services.load import load_for_activity  # noqa: E402

TOLERANCE = 1e-9


class Change(NamedTuple):
    activity: Activity
    old_load: float | None
    old_source: str | None
    new_load: float
    new_source: str


def _changed(old_load, old_src, new_load, new_src) -> bool:
    if old_src != new_src:
        return True
    if old_load is None:
        return abs(new_load) > TOLERANCE
    return abs(float(old_load) - new_load) > TOLERANCE


def collect_changes(db, athlete, limit: int = 0) -> list[Change]:
    """扫描全部活动，返回需要改动的行，并把新值写进 ORM 对象（尚未 commit）。"""
    query = db.query(Activity).order_by(Activity.start_time)
    if limit:
        query = query.limit(limit)

    changes: list[Change] = []
    for activity in query.all():
        old_load = activity.training_load
        old_source = (activity.raw or {}).get("training_load_source")
        new_load, new_source = load_for_activity(activity, athlete)
        if not _changed(old_load, old_source, new_load, new_source):
            continue
        # load_for_activity 只负责 raw；数值必须显式赋回列，
        # 否则 commit 只会刷 JSON 列、数值列静默保持旧值。
        activity.training_load = new_load
        changes.append(Change(activity, old_load, old_source, new_load, new_source))
    return changes


def apply_changes(db, changes: list[Change]) -> list[tuple]:
    """提交并回读校验，返回未按预期落盘的行（空列表表示全部成功）。"""
    db.commit()
    # 只信 commit() 是不够的 —— 属性没赋值时 commit 同样"成功"。
    db.expire_all()
    bad = []
    for ch in changes:
        fresh = db.get(Activity, ch.activity.id)
        got_load = fresh.training_load
        got_source = (fresh.raw or {}).get("training_load_source")
        if (got_load is None or abs(float(got_load) - ch.new_load) > TOLERANCE
                or got_source != ch.new_source):
            bad.append((ch.activity.id, got_load, got_source, ch.new_load, ch.new_source))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="按统一负荷引擎重算历史训练负荷")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只干跑报告）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条，便于试跑")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        athlete = db.query(Athlete).order_by(Athlete.id).first()
        if athlete is None:
            print("找不到运动员档案：无法按个体心率计算，终止（不做任何写入）。")
            return 2
        span = (athlete.max_hr or 0) - (athlete.resting_hr or 0)
        print(f"运动员 id={athlete.id} {athlete.name} sex={athlete.sex} "
              f"max_hr={athlete.max_hr} resting_hr={athlete.resting_hr}")
        hint = ("   ← 旧公式里写死的 140 恰好等于此值，属巧合而非通用常量"
                if span == 140 else "")
        print(f"  个体心率储备 = {span} bpm{hint}")
        print(f"模式: {'【写库】' if args.apply else '【干跑，不写任何数据】'}\n")

        changes = collect_changes(db, athlete, limit=args.limit)
        total = db.query(Activity).count()

        print(f"扫描 {total} 条活动，需要改动 {len(changes)} 条")
        if changes:
            print(f"\n  {'日期':11s} {'项目':10s} {'时长min':>7s} {'均心率':>6s} "
                  f"{'旧值':>9s} {'新值':>9s}  {'旧来源':>8s} -> {'新来源':<8s}")
            for ch in changes:
                a = ch.activity
                print(f"  {str(a.start_time or '')[:10]:11s} {str(a.sport)[:10]:10s} "
                      f"{(a.duration_sec or 0) / 60:7.1f} {str(a.avg_hr or '-'):>6s} "
                      f"{str(ch.old_load):>9s} {ch.new_load:9.1f}  "
                      f"{str(ch.old_source):>8s} -> {ch.new_source:<8s}")
        else:
            print("  已完全一致，无需改动（幂等 ✓）")

        if not args.apply:
            db.rollback()  # 丢弃干跑期间对 raw 的内存改动
            print("\n干跑结束，未写入任何数据。确认无误后加 --apply 执行。")
            return 0

        print("\n注意：请确认已对 .db 做过整库备份。")
        bad = apply_changes(db, changes)
        if bad:
            print("  ✗ 回读校验失败，以下行未按预期落盘：")
            for row in bad:
                print(f"    id={row[0]} 实际 load={row[1]} source={row[2]}，"
                      f"期望 load={row[3]} source={row[4]}")
            return 1
        print(f"  ✓ 已提交并回读校验通过：{len(changes)} 条均已在库中生效")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

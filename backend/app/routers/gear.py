"""装备/跑鞋跟踪：累计里程、寿命提醒与防伤联动。

防伤规则（tests/test_gear_injury.py 锁定）：旧鞋（磨损 ≥0.8 且 active）遇上
高负荷（ACWR > 1.3）才触发提醒，ACWR > 1.5 升级 high；新鞋/低负荷不触发。
单因素都不足以提醒——换鞋本身不伤，高负荷本身不伤，两者叠加才是风险窗口。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..data import get_default_athlete
from ..db import get_db

router = APIRouter(prefix="/api/gear", tags=["gear"])

ACWR_WARN = 1.3     # > 1.3 触发（恰好 1.3 不触发）
ACWR_HIGH = 1.5
WEAR_WORN = 0.8     # 磨损度视为「旧鞋」的分界


def _current_acwr(db: Session) -> float | None:
    """当前跑者的急性/慢性负荷比：近 7 天日均负荷 ÷ 近 28 天日均负荷。

    无档案或窗口内完全没有任何活动时返回 None（有档案没负荷 ≠ 负荷为 0，
    是「没有数据」，不该编一个比值）。
    """
    athlete = get_default_athlete(db)
    if not athlete:
        return None
    since = datetime.now() - timedelta(days=28)
    rows = db.execute(
        select(models.Activity.start_time, models.Activity.training_load).where(
            models.Activity.athlete_id == athlete.id,
            models.Activity.start_time >= since)).all()
    if not rows:
        return None
    now = datetime.now()
    acute = sum(load for st, load in rows
                if st >= now - timedelta(days=7) and load)
    chronic = sum(load for _, load in rows if load)
    if not chronic:
        return None
    return acute / 7 / (chronic / 28)


def _gear_dict(g, used_km: float, run_count: int, acwr: float | None = None) -> dict:
    """装备行 → 展示 dict：磨损度/剩余里程/寿命 flag/防伤提醒。

    g 可以是 ORM 对象或同字段替身（测试用 SimpleNamespace）。
    """
    total = (g.initial_km or 0) + (used_km or 0)
    wear = total / g.retire_km if g.retire_km else 0.0
    is_active = getattr(g, "status", "active") == "active"

    if wear >= 1 and is_active:
        flag, flag_note = "overdue", "已超退役里程，建议更换"
    elif wear >= WEAR_WORN and is_active:
        flag, flag_note = "warning", "接近退役里程，准备换鞋"
    else:
        flag, flag_note = "ok", ""

    injury = None
    if g.kind == "shoe" and is_active and wear >= WEAR_WORN and acwr is not None and acwr > ACWR_WARN:
        level = "high" if acwr > ACWR_HIGH else "medium"
        injury = {
            "level": level,
            "msg": (f"跑鞋磨损 {round(wear * 100)}% 且近期负荷偏高（ACWR {round(acwr, 2)}），"
                    "建议尽快换鞋，并适当下调训练强度"),
        }

    return {
        "id": g.id, "name": g.name, "kind": g.kind, "brand": g.brand,
        "start_date": g.start_date.isoformat() if g.start_date else None,
        "initial_km": g.initial_km, "retire_km": g.retire_km,
        "status": g.status, "notes": g.notes,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "used_km": round(used_km or 0, 1),
        "total_km": round(total, 1),
        "remaining_km": round(max(0.0, g.retire_km - total), 1),
        "wear_pct": round(min(1.5, wear) * 100),   # 封顶 150，超里程不爆表
        "run_count": run_count,
        "flag": flag, "flag_note": flag_note,
        "injury": injury,
    }


def _used_km_by_gear(db: Session, athlete_id: int) -> tuple[dict[int, float], dict[int, int]]:
    """按 gear 聚合活动里程与次数（activities.gear_id 关联，录入/同步时写入）。"""
    rows = db.execute(
        select(models.Activity.gear_id, models.Activity.distance_m, models.Activity.sport)
        .where(models.Activity.athlete_id == athlete_id,
               models.Activity.gear_id.isnot(None))).all()
    km: dict[int, float] = {}
    cnt: dict[int, int] = {}
    for gear_id, dist, _sport in rows:
        if dist and dist > 0:
            km[gear_id] = km.get(gear_id, 0) + dist / 1000
        cnt[gear_id] = cnt.get(gear_id, 0) + 1
    return km, cnt


@router.get("")
def list_gear(db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        return {"gears": [], "acwr": None}
    km, cnt = _used_km_by_gear(db, athlete.id)
    acwr = _current_acwr(db)
    rows = db.scalars(select(models.Gear).where(
        models.Gear.athlete_id == athlete.id).order_by(models.Gear.id.desc())).all()
    return {"gears": [_gear_dict(g, km.get(g.id, 0.0), cnt.get(g.id, 0), acwr=acwr) for g in rows],
            "acwr": round(acwr, 2) if acwr is not None else None}


@router.post("")
def add_gear(data: schemas.GearIn, db: Session = Depends(get_db)):
    athlete = get_default_athlete(db)
    if not athlete:
        raise HTTPException(404, "请先完善个人档案")
    row = models.Gear(athlete_id=athlete.id, **data.model_dump())
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@router.put("/{gear_id}")
def update_gear(gear_id: int, data: schemas.GearIn, db: Session = Depends(get_db)):
    row = db.get(models.Gear, gear_id)
    if not row:
        raise HTTPException(404, "装备不存在")
    for k, v in data.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return {"ok": True}


@router.post("/{gear_id}/retire")
def retire_gear(gear_id: int, db: Session = Depends(get_db)):
    """手动退役：退役后不再触发防伤提醒与寿命 flag。"""
    row = db.get(models.Gear, gear_id)
    if not row:
        raise HTTPException(404, "装备不存在")
    row.status = "retired"
    db.commit()
    return {"ok": True}


@router.delete("/{gear_id}")
def delete_gear(gear_id: int, db: Session = Depends(get_db)):
    row = db.get(models.Gear, gear_id)
    if not row:
        raise HTTPException(404, "装备不存在")
    # 关联活动只解除绑定，不删活动
    acts = db.scalars(select(models.Activity).where(models.Activity.gear_id == gear_id)).all()
    for a in acts:
        a.gear_id = None
    db.delete(row)
    db.commit()
    return {"ok": True}

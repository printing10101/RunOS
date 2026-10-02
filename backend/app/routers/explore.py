"""数据探索端点：白名单只读分析（引擎在 services/explore.py，与 AI 工具 query_data 共用）。

独立前端页已下线（2026-09-30，探索能力归 AI 教练的 query_data 工具），
端点保留作为引擎的 HTTP 面：test_explore_endpoint.py 经它锁定白名单红线。
红线不变：只接受结构化参数——参数形状在 pydantic（schemas.Explore*In），
表/列/聚合白名单在引擎（ValueError → 400），两层之外不存在任何 SQL 通道。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..services import explore

router = APIRouter(prefix="/api/explore", tags=["explore"])


@router.get("/schema")
def schema():
    """各表可探索的维度/指标元数据（含分类值的中文映射），前端据此渲染构建器。"""
    tables = []
    for key, t in explore.TABLES.items():
        dims = [{"key": d["key"], "label": d["label"],
                 **({"labels": d["labels"]} if d.get("labels") else {})}
                for d in t["dims"]]
        dims += [{"key": k, "label": lb,
                  **({"labels": explore.WEEKDAY_LABELS} if k == "weekday" else {})}
                 for k, (lb, _) in explore.GRAIN_DIMS.items()]
        tables.append({
            "key": key, "label": t["label"],
            "dims": dims,
            "measures": [{"key": m, "label": lb} for m, lb in t["measures"]],
        })
    return {"tables": tables}


@router.post("/query")
def run_query(body: schemas.ExploreQueryIn, db: Session = Depends(get_db)):
    """分组聚合：0-2 个维度 + 1-6 个指标。"""
    try:
        return explore.run_aggregate(
            db, body.table, body.group_by,
            [(m.col, m.agg) for m in body.metrics],
            [(f.col, f.op, f.value) for f in body.filters],
            body.date_from, body.date_to, body.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/rows")
def detail_rows(table: str, filters: str = "[]",
                date_from: date | None = None, date_to: date | None = None,
                limit: int = Query(default=200, ge=1, le=500),
                db: Session = Depends(get_db)):
    """明细下钻：按聚合行的维度值作 eq 筛选，回来看原始记录。"""
    try:
        return explore.run_detail(
            db, table, [(f.col, f.op, f.value) for f in _parse_filters(filters)],
            date_from, date_to, limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _parse_filters(filters_json: str) -> list[schemas.ExploreFilterIn]:
    """GET 端点的 filters 是 JSON 字符串参数，解析失败要给一句人话，不能 500。"""
    try:
        raw = json.loads(filters_json or "[]")
        return [schemas.ExploreFilterIn.model_validate(x) for x in raw]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"filters 不是合法的筛选数组：{exc}") from exc

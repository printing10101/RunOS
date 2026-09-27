"""路由层公共依赖。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..data import get_default_athlete


def require_athlete(db: Session) -> models.Athlete:
    """取默认跑者档案；未创建时统一 404。

    GET 类聚合接口（dashboard / training-status）需要返回 empty 结构以引导
    前端走「完善档案」流程，不经过这里；POST / 动作类接口一律走本依赖。
    """
    a = get_default_athlete(db)
    if not a:
        raise HTTPException(404, "请先完善个人档案")
    return a

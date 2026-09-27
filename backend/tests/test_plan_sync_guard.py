"""plan_sync「缺档案」守卫的回归测试。

背景：sync_coros_schedule 里 raise IntegrationError(...)，但模块从未导入它，
档案为空时点「同步高驰课表」会抛 NameError 变 500，而不是路由预期的
400 友好提示（ruff F821 检出，2026-09-27 修复后锁住本测试）。
"""
from __future__ import annotations

from datetime import date

import pytest
from app import models
from app.integrations.base import IntegrationError
from app.services import plan_sync
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _FakeAdapter:
    """只需让 fetch_training_schedule 返回非空列表；校验在读取字段之前就会触发。"""

    def fetch_training_schedule(self, start, end):
        return [{"date": date.today().isoformat(), "title": "轻松跑"}]


@pytest.fixture()
def db_no_athlete():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_sync_without_athlete_raises_integration_error(db_no_athlete):
    """库内没有档案时应抛 IntegrationError（路由转 400），绝不能再是 NameError。"""
    with pytest.raises(IntegrationError) as ei:
        plan_sync.sync_coros_schedule(db_no_athlete, _FakeAdapter())
    assert "档案" in str(ei.value)

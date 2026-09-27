"""自动同步调度：平台运行期间定时为已连接平台增量拉取新数据。

单进程桌面形态，一个可重置的 threading.Timer 足够（不为「准实时」引入
APScheduler 这样的常驻线程池）。规则与前端文案对齐（连接页）：
- 间隔 4-1440 分钟，存 app_settings，跨重启生效（settings.auto_sync_minutes
  只是初始默认）；
- 高频轮询只拉活动（时效所在）；睡眠/HRV 等身体数据一天只变几次，
  按至少 30 分钟一次降频（include_body 门控）。
"""
from __future__ import annotations

import threading
from datetime import datetime

from ..config import settings
from ..db import SessionLocal
from ..logging_config import get_logger

logger = get_logger(__name__)

MIN_MINUTES = 4
MAX_MINUTES = 1440
BODY_MIN_GAP_MIN = 30

_SETTINGS_ENABLED = "auto_sync_enabled"
_SETTINGS_MINUTES = "auto_sync_minutes"

# 最近一次自动同步结果：{platform: {at, ok, added, body_days, error}}
last_auto_result: dict[str, dict] = {}

_timer: threading.Timer | None = None
_lock = threading.Lock()
_running = False
_last_body_sync: datetime | None = None


class SyncBusy(RuntimeError):
    """上一次同步尚未结束。撞锁方直接跳过并提示，不排队。"""


def _read_setting(db, key: str, default: str) -> str:
    from sqlalchemy import select

    from .. import models as m
    row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == key))
    return row.value if row else default


def _write_setting(db, key: str, value: str) -> None:
    from sqlalchemy import select

    from .. import models as m
    row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == key))
    if not row:
        row = m.AppSetting(key=key)
        db.add(row)
    row.value = value


def auto_config(db) -> dict:
    """当前自动同步配置（前端连接页的初始状态）。"""
    enabled = _read_setting(db, _SETTINGS_ENABLED, "0") == "1"
    minutes = _read_setting(db, _SETTINGS_MINUTES, str(settings.auto_sync_minutes))
    try:
        minutes = int(minutes)
    except ValueError:
        minutes = settings.auto_sync_minutes
    return {"enabled": enabled, "minutes": minutes}


def set_auto_config(db, *, enabled: bool | None = None, minutes: int | None = None) -> dict:
    """修改配置并落库。间隔越界抛 ValueError（路由转 400）。"""
    cfg = auto_config(db)
    if minutes is not None:
        if not (MIN_MINUTES <= minutes <= MAX_MINUTES):
            raise ValueError(f"同步间隔需在 {MIN_MINUTES}-{MAX_MINUTES} 分钟之间")
        _write_setting(db, _SETTINGS_MINUTES, str(int(minutes)))
        cfg["minutes"] = int(minutes)
    if enabled is not None:
        _write_setting(db, _SETTINGS_ENABLED, "1" if enabled else "0")
        cfg["enabled"] = enabled
    db.commit()
    # 运行中改配置立即生效：按新间隔重新排队
    if _running:
        with _lock:
            _schedule_locked(cfg["minutes"] if cfg["enabled"] else None)
    return cfg


def _schedule_locked(minutes: int | None) -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    if minutes is None:
        return
    _timer = threading.Timer(minutes * 60, _run_once)
    _timer.daemon = True
    _timer.start()


def start_scheduler() -> None:
    """应用 lifespan 启动时调用：若自动同步是开启状态则开始轮询。"""
    global _running
    with _lock:
        _running = True
        db = SessionLocal()
        try:
            cfg = auto_config(db)
        finally:
            db.close()
        _schedule_locked(cfg["minutes"] if cfg["enabled"] else None)
    logger.info("自动同步调度器已启动（enabled=%s, minutes=%s）", cfg["enabled"], cfg["minutes"])


def stop_scheduler() -> None:
    """应用关闭时调用：撤销下一次排队。"""
    global _running, _timer
    with _lock:
        _running = False
        if _timer is not None:
            _timer.cancel()
            _timer = None
    logger.info("自动同步调度器已停止")


def _body_due(now: datetime) -> bool:
    global _last_body_sync
    if _last_body_sync is None:
        return True
    return (now - _last_body_sync).total_seconds() >= BODY_MIN_GAP_MIN * 60


def _run_once() -> None:
    """执行一轮自动同步，然后按当前配置排下一轮。任何单平台失败不拖垮其他平台。"""
    global _last_body_sync
    from sqlalchemy import select

    from .. import models as m

    # 延迟导入避免环：connections.py 只在函数内 import syncer，这里同理
    from ..routers.connections import execute_sync

    try:
        db = SessionLocal()
        try:
            rows = db.scalars(select(m.PlatformConnection).where(
                m.PlatformConnection.status == "connected")).all()
            include_body = _body_due(datetime.now())
            for row in rows:
                result = {"at": datetime.now().isoformat(timespec="seconds"), "ok": False}
                try:
                    synced = execute_sync(db, row, since_days=7, detail_limit=3,
                                          include_body=include_body)
                    result.update(ok=True, added=synced.get("added", 0),
                                  body_days=synced.get("body_days", 0))
                except SyncBusy:
                    logger.info("自动同步撞锁跳过 platform=%s", row.platform)
                    continue
                except Exception as exc:   # 单平台失败记录后继续下一个
                    result["error"] = str(exc)
                    logger.warning("自动同步失败 platform=%s: %s", row.platform, exc)
                last_auto_result[row.platform] = result
            if include_body:
                _last_body_sync = datetime.now()
        finally:
            db.close()
    except Exception:
        logger.exception("自动同步轮次异常")
    finally:
        with _lock:
            if _running:
                cfg_db = SessionLocal()
                try:
                    cfg = auto_config(cfg_db)
                finally:
                    cfg_db.close()
                _schedule_locked(cfg["minutes"] if cfg["enabled"] else None)

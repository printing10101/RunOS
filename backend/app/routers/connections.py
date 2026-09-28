"""平台连接与数据同步：高驰（MCP）/ 佳明 / Strava。

高驰走官方 MCP 通道：OAuth 2.0 授权码 + PKCE + 动态客户端注册，
凭据（client_id / token / 临时 PKCE 校验串）统一存 PlatformConnection.credentials。
"""
from __future__ import annotations

import html
import secrets
import threading
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from .. import models, schemas
from ..config import settings
from ..data import get_default_athlete
from ..db import get_db
from ..integrations.base import IntegrationError, PlatformAdapter
from ..integrations.coros import (
    CorosAdapter,
    parse_fitness_assessment,
    parse_training_load,
)
from ..integrations.garmin import GarminAdapter
from ..integrations.strava import StravaAdapter
from ..logging_config import get_logger, swallowed
from ..services.load import load_for_activity

logger = get_logger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])

ADAPTERS = {"coros": CorosAdapter, "garmin": GarminAdapter, "strava": StravaAdapter}

# 同步互斥锁：入库前有「查重 → 插入」的检查窗口，手动同步与后台自动同步并发
# 执行会把同一批活动插两遍。单进程桌面形态下进程内非阻塞锁即可覆盖，
# 撞锁方直接跳过并提示。
_SYNC_LOCK = threading.Lock()


def get_adapter(connection: models.PlatformConnection) -> PlatformAdapter:
    cls = ADAPTERS.get(connection.platform)
    if not cls:
        raise IntegrationError(f"未知平台 {connection.platform}")
    return cls(connection.credentials or {})


def _row_for(db: Session, platform: str) -> models.PlatformConnection | None:
    return db.scalar(select(models.PlatformConnection)
                     .where(models.PlatformConnection.platform == platform))


def _ensure_row(db: Session, platform: str) -> models.PlatformConnection:
    row = _row_for(db, platform)
    if not row:
        athlete = get_default_athlete(db)
        if not athlete:
            raise HTTPException(404, "请先完善个人档案")
        row = models.PlatformConnection(athlete_id=athlete.id, platform=platform, credentials={})
        db.add(row)
    if not isinstance(row.credentials, dict):
        row.credentials = {}
    return row


def _save_credentials(row: models.PlatformConnection, adapter: PlatformAdapter) -> None:
    """把适配器（可能被原地修改）的凭据写回 ORM 并标记为已变更。

    JSON 列的原地修改不会被 SQLAlchemy 自动检测，必须显式 flag_modified，
    否则 token 刷新结果不会落库。
    """
    row.credentials = dict(adapter.credentials)
    flag_modified(row, "credentials")


@router.get("/auto")
def auto_sync_status(db: Session = Depends(get_db)):
    """自动同步配置与最近一次自动同步结果（调度器由应用启动时拉起）。"""
    from ..services import syncer
    return {**syncer.auto_config(db), "last_run": syncer.last_auto_result}


@router.post("/auto")
def auto_sync_update(data: schemas.AutoSyncIn, db: Session = Depends(get_db)):
    """修改自动同步开关/间隔（持久化到 app_settings，跨重启生效）。"""
    from ..services import syncer
    try:
        cfg = syncer.set_auto_config(db, enabled=data.enabled, minutes=data.minutes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {**cfg, "last_run": syncer.last_auto_result}


@router.get("")
def list_connections(db: Session = Depends(get_db)):
    rows = db.scalars(select(models.PlatformConnection)).all()
    out = [{
        "id": c.id, "platform": c.platform, "mode": c.mode, "status": c.status,
        "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
    } for c in rows]

    coros_row = next((c for c in rows if c.platform == "coros"), None)
    coros_ready = bool(coros_row and coros_row.credentials.get("access_token"))

    config_ready = {
        # 高驰 MCP 通道无需预置凭据：客户端在首次授权时动态注册
        "coros": True,
        "garmin": bool((settings.garmin_email and settings.garmin_password) or settings.garmin_client_id),
        "strava": bool(settings.strava_client_id and settings.strava_client_secret),
    }
    return {"connections": out, "config_ready": config_ready,
            "coros_authorized": coros_ready}


@router.post("/connect")
def connect(data: schemas.ConnectIn, db: Session = Depends(get_db)):
    """连接平台：校验凭据可用性后记录连接（高驰请走 OAuth 授权）。"""
    if data.platform == "coros":
        raise HTTPException(400, "高驰使用 OAuth 授权接入，请点击「连接高驰」完成浏览器授权")
    cls = ADAPTERS.get(data.platform)
    if not cls:
        raise HTTPException(400, f"未知平台 {data.platform}")
    adapter = cls(data.credentials)
    try:
        adapter.check_config()
    except IntegrationError as e:
        raise HTTPException(400, str(e)) from e

    row = _ensure_row(db, data.platform)
    row.mode = data.mode
    row.status = "connected"
    row.credentials = data.credentials or row.credentials or {}
    flag_modified(row, "credentials")
    db.commit()
    return {"ok": True, "platform": data.platform, "mode": data.mode}


@router.post("/{platform}/authorize-url")
def authorize_url(platform: str, db: Session = Depends(get_db)):
    """生成授权跳转链接（高驰走 MCP OAuth：动态注册客户端 + PKCE）。"""
    if platform not in ("coros", "strava"):
        raise HTTPException(400, "该平台无需 OAuth 跳转")

    if platform == "coros":
        row = _ensure_row(db, "coros")
        adapter = CorosAdapter(row.credentials)
        try:
            info = adapter.begin_authorize()
        except IntegrationError as e:
            db.rollback()
            raise HTTPException(400, str(e)) from e
        _save_credentials(row, adapter)
        row.mode = "mcp"
        # 已连接用户再次点授权时不把可用 token 打回 pending，
        # 仅对非连接态标记，避免授权中途放弃把连接变回未连接
        if row.status != "connected":
            row.status = "pending"
        db.commit()
        return {"url": info["url"], "state": info["state"]}

    # Strava：生成随机 state 存入连接记录，回调时 fail-closed 校验
    # （固定常量 state 等于没有 CSRF 防护，可被置换授权码错绑账号）
    row = _ensure_row(db, "strava")
    state = secrets.token_urlsafe(16)
    row.credentials = {**(row.credentials or {}), "oauth_state": state}
    flag_modified(row, "credentials")
    if row.status != "connected":
        row.status = "pending"
    try:
        url = StravaAdapter({}).authorize_url(state=state)
    except IntegrationError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"url": url, "state": state}


@router.get("/coros/callback", response_class=HTMLResponse)
def coros_callback(code: str = Query(default=""), state: str = Query(default=""),
                   error: str = Query(default=""), error_description: str = Query(default=""),
                   db: Session = Depends(get_db)):
    """高驰 OAuth 回调：校验 state、换取令牌并入库。"""
    if error:
        return _page("授权未完成",
                     f"高驰返回错误：{html.escape(error_description or error)}", ok=False)
    if not code:
        return _page("授权未完成", "未收到授权码，请重新发起授权。", ok=False)

    row = _row_for(db, "coros")
    if not row:
        return _page("授权未完成", "未找到授权会话，请先在「平台连接」页点击「连接高驰」。", ok=False)

    adapter = CorosAdapter(row.credentials)
    try:
        adapter.complete_authorize(code, state)
    except IntegrationError as e:
        db.rollback()
        return _page("授权失败", html.escape(str(e)), ok=False)

    _save_credentials(row, adapter)
    row.mode = "mcp"
    row.status = "connected"
    row.last_sync_at = None
    db.commit()
    return _page("高驰授权成功", "已连接你的高驰账号，可以关闭本页回到平台点击「同步数据」。", ok=True)


@router.get("/coros/tools")
def coros_tools(db: Session = Depends(get_db)):
    """诊断：列出高驰 MCP 当前可用的工具（确认通道与授权是否正常）。"""
    row = _row_for(db, "coros")
    if not row or not row.credentials.get("access_token"):
        raise HTTPException(400, "尚未连接高驰账号")
    adapter = CorosAdapter(row.credentials)
    try:
        tools = adapter.list_tools()
    except IntegrationError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        _save_credentials(row, adapter)
        db.commit()
    return {
        "count": len(tools),
        "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:120],
                   "params": list(((t.get("inputSchema") or {}).get("properties") or {}).keys())}
                  for t in tools],
    }


@router.get("/coros/fitness")
def coros_fitness(db: Session = Depends(get_db)):
    """高驰官方体能评估实时透传：VO2max / 阈值配速 / 赛事预测 / 训练负荷比。

    同步链会把评估按日快照进 fitness_snapshots（趋势与 AI 工具用库内数据）；
    本端点仍实时拉取原文，供连接页即时查看与解析器的人工核对。
    """
    row = _row_for(db, "coros")
    if not row or not row.credentials.get("access_token"):
        raise HTTPException(400, "尚未连接高驰账号")
    adapter = CorosAdapter(row.credentials)
    try:
        adapter.check_config()
        return adapter.fetch_fitness_assessment()
    except IntegrationError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        _save_credentials(row, adapter)
        db.commit()


@router.get("/strava/callback", response_class=HTMLResponse)
def strava_callback(code: str = Query(default=""), state: str = Query(default=""),
                    error: str = Query(default=""), error_description: str = Query(default=""),
                    db: Session = Depends(get_db)):
    """Strava OAuth 回调：校验 state（fail-closed）、换取令牌并入库，HTML 结果页收尾。"""
    if error:
        return _page("授权未完成",
                     f"Strava 返回错误：{html.escape(error_description or error)}", ok=False)
    if not code:
        return _page("授权未完成", "未收到授权码，请重新发起授权。", ok=False)
    row = _row_for(db, "strava")
    if not row:
        return _page("授权未完成", "未找到授权会话，请先在「平台连接」页发起 Strava 授权。", ok=False)
    saved_state = str((row.credentials or {}).get("oauth_state") or "")
    if not saved_state or not state or not secrets.compare_digest(saved_state, state):
        return _page("授权失败", "state 校验失败（会话不匹配或已过期），请重新发起授权。", ok=False)

    adapter = StravaAdapter({})
    try:
        token = adapter.exchange_token(code)
    except IntegrationError as e:
        db.rollback()
        return _page("授权失败", html.escape(str(e)), ok=False)

    creds = {k: v for k, v in (row.credentials or {}).items() if k != "oauth_state"}
    row.credentials = {**creds, **token}
    flag_modified(row, "credentials")
    row.mode = "official"
    row.status = "connected"
    row.last_sync_at = None
    db.commit()
    return _page("Strava 授权成功", "已连接你的 Strava 账号，可以关闭本页回到平台点击「同步数据」。", ok=True)


@router.post("/{platform}/sync")
def sync_platform(platform: str, data: schemas.SyncIn, db: Session = Depends(get_db)):
    """拉取活动入库（含卡路里/功率/训练效果/分段）+ 同步每日身体数据。"""
    row = _row_for(db, platform)
    if not row or row.status != "connected":
        raise HTTPException(400, f"{platform} 尚未连接")
    try:
        return execute_sync(db, row, since_days=data.since_days,
                            detail_limit=data.detail_limit, backfill_detail=data.backfill_detail)
    except IntegrationError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/coros/sync-schedule")
def sync_coros_schedule(db: Session = Depends(get_db)):
    """镜像高驰官方课表到本地训练计划（高驰为课表事实源）。

    与活动同步共用互斥锁：课表 upsert 与活动入库都写 plan_workouts/activities
    侧的会话状态，并发执行没有意义还会拖慢双方。
    """
    row = _row_for(db, "coros")
    if not row or row.status != "connected":
        raise HTTPException(400, "尚未连接高驰账号")
    if not _SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(409, "上一次同步尚未结束，请稍后再试")
    adapter = CorosAdapter(row.credentials)
    try:
        from ..services import plan_sync
        result = plan_sync.sync_coros_schedule(db, adapter)
        db.commit()
        return result
    except IntegrationError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    finally:
        _save_credentials(row, adapter)
        db.commit()
        _SYNC_LOCK.release()


def execute_sync(db: Session, row: models.PlatformConnection, *, since_days: int,
                 detail_limit: int = 10, backfill_detail: bool = False,
                 include_body: bool = True) -> dict:
    """执行一次完整同步（手动同步路由与后台自动调度共用同一实现）。

    凭据失效等 IntegrationError 在保存最新凭据后原样上抛，由调用方决定如何呈现；
    单条详情/身体数据的失败在内部吞掉并记日志，不阻断整次同步。
    include_body=False 时跳过身体数据（自动调度的高频轮询用：活动才是时效所在，
    睡眠/静息心率等一天只变几次，由调度方降频控制，见 services/syncer.py）。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        from ..services.syncer import SyncBusy  # 延迟导入避免循环依赖
        raise SyncBusy("上一次同步尚未结束，请稍后再试")
    try:
        return _execute_sync_locked(db, row, since_days=since_days,
                                    detail_limit=detail_limit, backfill_detail=backfill_detail,
                                    include_body=include_body)
    finally:
        _SYNC_LOCK.release()


def _fetch_activities_paged(adapter: Any, since_days: int) -> list:
    """分段滚动拉取 + 批内去重合并。

    高驰 querySportRecords 单次返回有条数上限（约 80 条，升序、静默截断），
    长窗口会截掉最新的活动。按 60 天一段从现在往回滚动拉取，去重合并，
    直到覆盖 since_days 或确认没有更早的数据。
    """
    acts: list = []
    seen_keys: set = set()
    now = datetime.now()
    floor = now - timedelta(days=since_days)
    seg_end = now
    for _ in range(8):  # 上限防失控：60 天 × 8 段 = 480 天
        seg_start = max(floor, seg_end - timedelta(days=60))
        seg = adapter.fetch_activities(seg_start, seg_end)
        before = len(seen_keys)
        for a in seg:
            key = a.external_id or (a.sport, a.start_time.isoformat() if a.start_time else id(a))
            if key not in seen_keys:
                seen_keys.add(key)
                acts.append(a)
        if seg_start <= floor or not seg or len(seen_keys) == before:
            break  # 已覆盖到目标起点 / 该段无数据 / 该段全部重复
        seg_end = seg_start
    return acts


def _filter_new_activities(db: Session, row: models.PlatformConnection, acts: list) -> list:
    """过滤掉库里已有的活动，返回本次要入库的新活动。"""
    # execute（而非 scalars）：scalars 只返回每行第一列，下方按 (id, sport, start) 三元组解包会崩
    existing = db.execute(
        select(models.Activity.external_id, models.Activity.sport, models.Activity.start_time)
        .where(models.Activity.platform == row.platform)).all()
    # 优先用平台 external_id 去重；对缺失 id（空串/None）的活动，退化为 (sport, 开始时间) 键，
    # 与批内 seen_keys 规则一致，避免每次同步把同一条无 id 活动重复入库
    existing_ids = {e for (e, _, _) in existing if e}
    existing_ks = {(s, st.isoformat()) for (_, s, st) in existing if st}
    return [a for a in acts
            if not (a.external_id and a.external_id in existing_ids)
            and not (not a.external_id and a.start_time
                     and (a.sport, a.start_time.isoformat()) in existing_ks)]


def _prioritize_recent(new_acts: list, recent_hours: int = 48) -> list:
    """把最近的活动排到队首（不改变内容，只影响富化顺序）。

    自动同步每轮只富化 detail_limit 条：平台返回顺序不保证最新在前，
    不排序时「今天刚跑完的课」可能抢不到名额——而它恰恰是调课分析
    （workout_analysis / plan_drift）最需要逐公里分段和心率细节的一条。
    """
    now = datetime.now()

    def _rank(a: Any):
        recent = bool(a.start_time) and (now - a.start_time) <= timedelta(hours=recent_hours)
        ts = a.start_time.timestamp() if a.start_time else 0.0
        return (0 if recent else 1, -ts)

    return sorted(new_acts, key=_rank)


def _enrich_new_activities(adapter: Any, new_acts: list, detail_limit: int) -> None:
    """为最近若干条新活动补拉详情（分段/轨迹/曲线），失败不阻断同步。"""
    fetch_detail = getattr(adapter, "fetch_detail", None)
    apply_detail = getattr(adapter, "apply_detail", None)
    fetch_laps = getattr(adapter, "fetch_laps", None)
    apply_laps = getattr(adapter, "apply_laps", None)
    limit = max(0, min(int(detail_limit), 500))
    if not fetch_detail:
        return
    for a in new_acts[:limit]:
        with swallowed("补拉活动详情", logger=logger, activity=a.external_id):
            det = fetch_detail(a.external_id)
            if det:
                a.raw["detail"] = det
                if apply_detail:
                    apply_detail(a, det.get("text") or "")
            # 圈数据（逐公里分段/心率/功率/跑步动态）与详情同源同额度，一并拉取
            if fetch_laps and apply_laps:
                laps = fetch_laps(a.external_id, (a.raw or {}).get("sport_type"))
                apply_laps(a, laps)


def _backfill_activity_details(db: Session, adapter: Any, athlete: object,
                               platform: str, detail_limit: int,
                               backfill_detail: bool) -> int:
    """历史活动详情回填：早期同步只存了列表字段，步频/功率/训练效果等列为空，此处补齐。"""
    fetch_detail = getattr(adapter, "fetch_detail", None)
    apply_detail = getattr(adapter, "apply_detail", None)
    limit = max(0, min(int(detail_limit), 500))
    backfilled = 0
    if not (fetch_detail and apply_detail and backfill_detail and limit):
        return 0
    stale = db.scalars(
        select(models.Activity).where(
            models.Activity.platform == platform,
            models.Activity.sport.in_(("run", "ride", "swim")),
            models.Activity.avg_cadence.is_(None),
            models.Activity.external_id.isnot(None),
        ).order_by(models.Activity.start_time.desc()).limit(limit)
    ).all()
    for act in stale:
        with swallowed("回填活动详情", logger=logger, activity=act.external_id):
            sport_code = (act.raw or {}).get("sport_type")
            det = fetch_detail(act.external_id, sport_code)
            if not det:
                continue
            act.raw = dict(act.raw or {})
            act.raw["detail"] = det
            apply_detail(act, det.get("text") or "")
            # 负荷走唯一取值路径，保持 raw 里的来源标签与数值一致
            load, _ = load_for_activity(act, athlete)
            act.training_load = load
            backfilled += 1
    return backfilled


def _backfill_activity_laps(db: Session, adapter: Any, platform: str,
                            detail_limit: int, backfill_detail: bool) -> int:
    """历史活动圈数据回填：解耦/疲劳抗性/真实分段依赖逐圈数据，跑步行列优先。

    用 laps_fetched_at 标记防止空返回的活动每轮反复烧配额。
    """
    fetch_laps = getattr(adapter, "fetch_laps", None)
    apply_laps = getattr(adapter, "apply_laps", None)
    limit = max(0, min(int(detail_limit), 500))
    laps_backfilled = 0
    if not (fetch_laps and apply_laps and backfill_detail and limit):
        return 0
    candidates = db.scalars(
        select(models.Activity).where(
            models.Activity.platform == platform,
            models.Activity.sport.in_(("run", "walk")),
            models.Activity.external_id.isnot(None),
        ).order_by(models.Activity.start_time.desc()).limit(limit * 3)
    ).all()
    for act in candidates:
        raw = act.raw or {}
        if raw.get("splits") or raw.get("laps_fetched_at"):
            continue
        with swallowed("回填活动圈数据", logger=logger, activity=act.external_id):
            laps = fetch_laps(act.external_id, raw.get("sport_type"))
            act.raw = dict(raw)
            act.raw["laps_fetched_at"] = datetime.now().isoformat(timespec="seconds")
            if apply_laps(act, laps):
                laps_backfilled += 1
        if laps_backfilled >= limit:
            break
    return laps_backfilled


def _sync_body_metrics(db: Session, row: models.PlatformConnection, adapter: Any,
                       include_body: bool, since_days: int) -> int:
    """同步身体数据（窗口跟随同步范围，上限 365 天），返回写入天数。"""
    fetch_body = getattr(adapter, "fetch_body_metrics", None)
    if not (fetch_body and include_body):
        return 0
    try:
        body_synced = 0
        # 接口若对大窗口截断，以实际返回天数为准
        for m in fetch_body(min(since_days, 365)):
            _upsert_body_metric(db, row.athlete_id, m)
            body_synced += 1
        return body_synced
    except IntegrationError:
        # 授权失效：凭据/进度先落库再上抛，下一轮同步从本次进度续传
        _save_credentials(row, adapter)
        db.commit()
        raise
    except Exception as exc:
        # 身体数据接口失败不阻断活动同步。这里不能用 swallowed()：
        # 上面的 except 要求 IntegrationError 继续上抛，swallowed 会把它一并吞掉。
        logger.warning("同步身体数据失败，不阻断活动同步: %s", exc)
        db.rollback()   # 半写的 body metric 一并放弃，避免脏会话影响后续
        return 0


def _sync_fitness_snapshots(db: Session, row: models.PlatformConnection, adapter: Any,
                            include_body: bool, since_days: int) -> int:
    """官方体能快照（VO2max/阈值配速/赛事预测/负荷比）每日一次落 fitness_snapshots。

    queryTrainingLoadAssessment 自带逐日历史，首次同步即可回填出趋势线。
    """
    fetch_fitness = getattr(adapter, "fetch_fitness_assessment", None)
    if not (fetch_fitness and include_body) or _snapshot_up_to_date(db, row.athlete_id):
        return 0
    try:
        fitness_synced = 0
        texts = fetch_fitness(since_days=min(since_days, 90))
        for day, fields in parse_training_load(texts.get("load") or "").items():
            _upsert_fitness_snapshot(db, row.athlete_id, day, fields)
            fitness_synced += 1
        fitness = parse_fitness_assessment(texts.get("fitness") or "")
        if fitness:
            _upsert_fitness_snapshot(db, row.athlete_id, date.today(), fitness)
            fitness_synced += 1
        return fitness_synced
    except IntegrationError:
        _save_credentials(row, adapter)
        db.commit()
        raise
    except Exception as exc:
        logger.warning("同步官方体能快照失败，不阻断同步: %s", exc)
        db.rollback()
        return 0


def _sync_recovery_status(db: Session, row: models.PlatformConnection, adapter: Any,
                          include_body: bool) -> bool:
    """官方恢复状态（当期快照）→ 当日 body metric，与引擎估算恢复时间互为对照。"""
    fetch_recovery = getattr(adapter, "fetch_recovery_status", None)
    if not (fetch_recovery and include_body):
        return False
    try:
        rec = fetch_recovery()
        if rec:
            rec["date"] = date.today().isoformat()
            _upsert_body_metric(db, row.athlete_id, rec)
            return True
    except Exception as exc:
        logger.warning("同步官方恢复状态失败，不阻断同步: %s", exc)
        db.rollback()
    return False


def _execute_sync_locked(db: Session, row: models.PlatformConnection, *, since_days: int,
                         detail_limit: int, backfill_detail: bool,
                         include_body: bool = True) -> dict:
    """同步主流程编排：拉取 → 去重 → 详情富化/历史回填 → 入库 → 身体/快照/恢复。

    各阶段独立成函数；编排层只负责顺序与「活动先落盘，身体数据失败不回滚活动」的边界。
    """
    adapter = get_adapter(row)
    athlete = db.get(models.Athlete, row.athlete_id)
    try:
        # 告诉适配器哪些活动已在库内：详情富化（分段/轨迹/曲线，每条 3 次请求）
        # 只对即将入库的新活动有意义，跳过老活动避免自动同步反复烧平台配额
        dup_rows = db.execute(
            select(models.Activity.external_id)
            .where(models.Activity.platform == row.platform)).all()
        adapter.skip_enrich_ids = {e for (e,) in dup_rows if e}
        adapter.check_config()
        acts = _fetch_activities_paged(adapter, since_days)
    except Exception:
        # 令牌刷新等中间态可能只发生在内存凭据里，失败时也要落库，
        # 否则 Strava 的 refresh token 轮换后旧值作废，连接失效。
        try:
            _save_credentials(row, adapter)
            db.commit()
        except Exception:
            logger.exception("同步失败时回写凭据失败 platform=%s", row.platform)
        raise

    new_acts = _filter_new_activities(db, row, acts)
    # 详情/圈数据富化配额每轮只有 detail_limit 个名额：排序保证最近 48h 的活动
    # （刚跑完的课，调课分析最依赖它的分段/心率细节）优先拿到名额
    new_acts = _prioritize_recent(new_acts)
    _enrich_new_activities(adapter, new_acts, detail_limit)
    backfilled = _backfill_activity_details(db, adapter, athlete,
                                            row.platform, detail_limit, backfill_detail)
    laps_backfilled = _backfill_activity_laps(db, adapter, row.platform,
                                              detail_limit, backfill_detail)

    added = 0
    new_ids: list[int] = []
    for a in new_acts:
        obj = _insert_activity(db, row, a)
        if obj is not None:
            added += 1
            new_ids.append(obj.id)

    # 活动先落盘：身体数据阶段失败（授权失效等）不能回滚整批已拉取的活动
    _save_credentials(row, adapter)
    row.last_sync_at = datetime.now()
    db.commit()

    body_synced = _sync_body_metrics(db, row, adapter, include_body, since_days)
    fitness_synced = _sync_fitness_snapshots(db, row, adapter, include_body, since_days)
    recovery_synced = _sync_recovery_status(db, row, adapter, include_body)

    db.commit()

    # 同步后置钩子（活动↔课表对账 / 训练后点评 / 画像刷新）：必须在上面的活动
    # commit 之后跑；失败只记日志，不影响同步结果本身
    if new_ids:
        try:
            from ..services.activity_link import on_activities_changed
            on_activities_changed(db, row.athlete_id, new_ids)
        except Exception:
            logger.exception("同步后置处理失败（不影响同步本身） athlete=%s", row.athlete_id)

    return {"ok": True, "fetched": len(acts), "added": added,
            "backfilled": backfilled, "laps_backfilled": laps_backfilled,
            "body_days": body_synced, "fitness_days": fitness_synced,
            "recovery": recovery_synced}


def _training_load(a: Any, athlete: object = None) -> float:
    """训练负荷：走统一引擎的唯一取值路径（真实值 > TRIMP > sRPE > unknown）。

    优先级在 services.load.load_for_activity 里，此处只做转发，
    线上与重算脚本 (tools/recalc_training_load.py) 用同一份优先级。
    """
    load, _ = load_for_activity(a, athlete)
    return load


def _quarantine_bad_distance(a: Any) -> None:
    """源平台 GPS 漂移可能返回荒谬距离。

    足部运动均速 > 40 km/h、骑行 > 100 km/h 视为距离字段损坏：距离清零入库、
    原值存 raw.distance_quarantined 留痕。距离为 0 后界面显示「-」，
    且不会污染跑量/配速/PB 等任何统计。
    """
    if not getattr(a, "distance_m", None) or not getattr(a, "duration_sec", None):
        return
    kmh = (a.distance_m / 1000) / (a.duration_sec / 3600)
    limit = 100 if getattr(a, "sport", "") == "ride" else 40
    if kmh > limit:
        a.raw = {**(getattr(a, "raw", None) or {}), "distance_quarantined": a.distance_m}
        a.distance_m = 0


def _insert_activity(db: Session, row: models.PlatformConnection, a: Any) -> models.Activity | None:
    """单条活动入库；命中平台去重唯一索引（ux_activities_platform_external）时
    返回 None 而不是让整批失败。

    应用层 existing_ids 去重只能拦「同步开始前已在库」的活动；多开实例并发拉取时
    两边都可能通过应用层检查，DB 层唯一索引是最后一道闸。SAVEPOINT 保证只回滚
    本条，先前入队的活动不受影响。
    """
    _quarantine_bad_distance(a)
    obj = models.Activity(
        athlete_id=row.athlete_id, external_id=a.external_id, platform=row.platform,
        sport=a.sport, title=a.title, start_time=a.start_time,
        duration_sec=a.duration_sec, distance_m=a.distance_m, avg_hr=a.avg_hr,
        max_hr=a.max_hr, elevation_m=a.elevation_m, avg_cadence=a.avg_cadence,
        avg_power=a.avg_power, calories=a.calories, temp_c=a.temp_c,
        weather=a.weather, te_aerobic=a.te_aerobic, te_anaerobic=a.te_anaerobic,
        dynamics=a.dynamics,
        training_load=_training_load(a),
        effort_score=round((a.distance_m / max(1, a.duration_sec)) * 3600 / 1000, 2),
        raw=a.raw,
    )
    try:
        with db.begin_nested():
            db.add(obj)
            db.flush()
    except IntegrityError:
        if obj in db:
            db.expunge(obj)
        logger.warning("活动命中唯一索引，按并发重复跳过 platform=%s external_id=%s",
                       row.platform, a.external_id)
        return None
    return obj


def _snapshot_up_to_date(db: Session, athlete_id: int) -> bool:
    """今天的体能快照已在库则跳过拉取（官方评估一天内基本不变，降频到每日一次）。"""
    return db.scalar(select(models.FitnessSnapshot.id).where(
        models.FitnessSnapshot.athlete_id == athlete_id,
        models.FitnessSnapshot.date == date.today())) is not None


def _upsert_fitness_snapshot(db: Session, athlete_id: int, day: date, fields: dict) -> None:
    """按 (athlete_id, date) upsert 快照；None 字段不覆盖已有值。"""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if not day:
        return
    row = db.scalar(select(models.FitnessSnapshot).where(
        models.FitnessSnapshot.athlete_id == athlete_id,
        models.FitnessSnapshot.date == day))
    if not row:
        row = models.FitnessSnapshot(athlete_id=athlete_id, date=day)
        db.add(row)
        # autoflush=False：同一天负荷历史和体能评估会先后 upsert 同一行，
        # 不 flush 的话第二个 select 看不到 pending 行，撞唯一约束
        db.flush()
    for k, v in fields.items():
        if k == "date" or v is None:
            continue
        if hasattr(row, k):
            setattr(row, k, v)


def _upsert_body_metric(db: Session, athlete_id: int, m: dict) -> None:
    d = m.get("date")
    d = date.fromisoformat(d) if isinstance(d, str) else d
    if not d:
        return
    # 官方恢复状态的原始键名（parse_recovery_status）与 body_metrics 列名对齐：
    # level → recovery_level、full_recovery_hours → recovery_hours
    key_alias = {"level": "recovery_level", "full_recovery_hours": "recovery_hours"}
    row = db.scalar(select(models.BodyMetric).where(
        models.BodyMetric.athlete_id == athlete_id, models.BodyMetric.date == d))
    if not row:
        row = models.BodyMetric(athlete_id=athlete_id, date=d)
        db.add(row)
        db.flush()   # autoflush=False：同一会话内同一天的后续 upsert 要能看到这行
    for k, v in m.items():
        k = key_alias.get(k, k)
        if k == "date" or v is None:
            continue
        if hasattr(row, k):
            setattr(row, k, v)


@router.post("/{platform}/disconnect")
def disconnect(platform: str, db: Session = Depends(get_db)):
    row = _row_for(db, platform)
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/reset-data")
def reset_data(confirm: bool = False, db: Session = Depends(get_db)):
    """清空训练/身体/评估等业务数据（用于清除演示数据、以真实数据重新开始）。

    保留：平台连接（已授权 token 不丢）、跑者档案/目标/日程（用户配置）。
    前端二次确认后调用。
    """
    if not confirm:
        raise HTTPException(400, "请携带 confirm=true 确认执行")
    cleared = {}
    for name, model in (("activities", models.Activity), ("gears", models.Gear),
                        ("race_results", models.RaceResult), ("daily_checkins", models.DailyCheckin),
                        ("body_metrics", models.BodyMetric),
                        ("strength_tests", models.StrengthTest), ("diet_logs", models.DietLog),
                        ("assessments", models.Assessment), ("plan_workouts", models.PlanWorkout),
                        ("plan_weeks", models.PlanWeek), ("training_plans", models.TrainingPlan)):
        rows = db.scalars(select(model)).all()
        for r in rows:
            db.delete(r)
        cleared[name] = len(rows)
    db.commit()
    return {"ok": True, "cleared": cleared}


def _page(title: str, message: str, ok: bool) -> str:
    """给浏览器回调页用的简单结果页。"""
    color = "#16a34a" if ok else "#dc2626"
    icon = "✓" if ok else "✕"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title)}</title></head>
<body style="margin:0;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center;max-width:520px;padding:32px">
<div style="width:64px;height:64px;line-height:64px;border-radius:50%;margin:0 auto 20px;
background:{color}22;color:{color};font-size:32px">{icon}</div>
<h1 style="font-size:20px;margin:0 0 12px">{html.escape(title)}</h1>
<p style="color:#94a3b8;line-height:1.7;margin:0">{message}</p>
</div></body></html>"""

"""数据探索引擎：白名单表上的只读分组聚合与明细查询。

护栏（产品红线）：端点/工具只接受结构化参数——表、列、操作符三重白名单。
所有合法的分组/聚合/筛选 SQL 表达式都在**导入期**预编译进注册表
（_exprs / _agg_exprs / _detail_cols），调用方只做 ``dict.get`` 选表达式，
用户输入永远不参与 SQL 构建；筛选值一律经 SQLAlchemy 绑定参数下发，
**绝无裸 SQL、绝无字符串拼 SQL**。所有查询强制圈定默认档案（与 stats
路由同口径）。量级是单档案的训练数据，聚合/明细由 limit 截断（响应带
truncated 标记）。

消费方：routers/explore.py（HTTP 端点，锁定测试 test_explore_endpoint.py）
与 AI 教练工具 query_data（services/ai_tools.py）。白名单校验失败一律
raise ValueError——端点转 400、工具转 ok=False，两条路共用同一道闸门；
错误信息带可用选项清单，LLM 拿到即可自纠错重试。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..data import get_default_athlete
from .injury import SEVERITY_LABELS as _INJURY_SEVERITY_LABELS
from .vocab import SESSION_TYPE_LABELS

# 日期维度保留字：任何表都能按自己日期列的 日/月/星期 分组与筛选。
# 右侧表达式工厂以日期列实参调用，同一套实现同时吃 DateTime 与 Date 列
# （SQLite 里两者都是 ISO 字符串，date/strftime 直接可用）。
GRAIN_DIMS: dict[str, tuple[str, object]] = {
    "day": ("日期", lambda col: func.date(col)),
    "month": ("月份", lambda col: func.strftime("%Y-%m", col)),
    "weekday": ("星期", lambda col: func.strftime("%w", col)),
}
# strftime("%w") 的返回：0=周日
WEEKDAY_LABELS = {"0": "周日", "1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六"}

_SPORT_LABELS = {"run": "跑步", "ride": "骑行", "swim": "游泳", "strength": "力量",
                 "walk": "步行", "other": "其他"}
_WORKOUT_STATUS_LABELS = {"planned": "计划中", "synced": "已下发",
                          "completed": "已完成", "skipped": "已跳过"}
_COMPLETED_SOURCE_LABELS = {"manual": "手动打卡", "linked": "关联活动"}

# 表注册表：dims/measures/details 的每一列都是显式白名单——未登记的
# 表/列一律 ValueError，这是防注入的第一道也是唯一一道闸门，加列必须在这里登记。
TABLES: dict[str, dict] = {
    "activities": {
        "label": "训练活动",
        "model": models.Activity,
        "date_col": "start_time",
        "dims": [
            {"key": "sport", "label": "运动类型", "labels": _SPORT_LABELS},
            {"key": "platform", "label": "来源平台"},
        ],
        "measures": [
            ("distance_m", "距离(米)"), ("duration_sec", "时长(秒)"),
            ("training_load", "训练负荷"), ("avg_hr", "平均心率"), ("max_hr", "最大心率"),
            ("calories", "千卡"), ("elevation_m", "爬升(米)"), ("avg_cadence", "步频"),
            ("avg_power", "功率"), ("rpe", "主观强度"), ("effort_score", "相对强度分"),
            ("te_aerobic", "有氧训练效果"), ("te_anaerobic", "无氧训练效果"),
        ],
        "details": [
            ("start_time", "时间"), ("title", "标题"), ("sport", "运动类型"),
            ("platform", "平台"), ("distance_m", "距离(米)"), ("duration_sec", "时长(秒)"),
            ("avg_hr", "平均心率"), ("training_load", "负荷"), ("calories", "千卡"), ("rpe", "RPE"),
        ],
    },
    "plan_workouts": {
        "label": "计划课表",
        "model": models.PlanWorkout,
        "date_col": "date",
        "dims": [
            {"key": "session_type", "label": "课型", "labels": SESSION_TYPE_LABELS},
            {"key": "status", "label": "完成状态", "labels": _WORKOUT_STATUS_LABELS},
            {"key": "completed_source", "label": "完成方式", "labels": _COMPLETED_SOURCE_LABELS},
        ],
        "measures": [("distance_km", "计划距离(km)"), ("duration_min", "计划时长(分)")],
        "details": [
            ("date", "日期"), ("title", "课名"), ("session_type", "课型"), ("status", "状态"),
            ("distance_km", "计划距离(km)"), ("duration_min", "计划时长(分)"),
            ("completed_source", "完成方式"),
        ],
    },
    "daily_checkins": {
        "label": "每日打卡",
        "model": models.DailyCheckin,
        "date_col": "date",
        "dims": [],
        "measures": [
            ("sleep_quality", "睡眠质量(1-5)"), ("muscle_soreness", "酸痛(0-5)"),
            ("energy_level", "精力(1-5)"), ("motivation", "训练意愿(1-5)"),
        ],
        "details": [
            ("date", "日期"), ("sleep_quality", "睡眠质量"), ("muscle_soreness", "酸痛"),
            ("energy_level", "精力"), ("motivation", "训练意愿"),
            ("pain_area", "疼痛部位"), ("note", "备注"),
        ],
    },
    "body_metrics": {
        "label": "身体数据",
        "model": models.BodyMetric,
        "date_col": "date",
        "dims": [{"key": "recovery_level", "label": "官方恢复分级"}],
        "measures": [
            ("weight_kg", "体重(kg)"), ("resting_hr", "静息心率"), ("hrv_rmssd", "HRV"),
            ("sleep_hours", "睡眠(小时)"), ("body_fat_pct", "体脂(%)"), ("sleep_score", "睡眠分数"),
            ("spo2", "血氧(%)"), ("resp_rate", "呼吸率"), ("stress", "压力分数"),
            ("body_battery", "身体电量"), ("recovery_pct", "恢复(%)"),
        ],
        "details": [
            ("date", "日期"), ("weight_kg", "体重(kg)"), ("resting_hr", "静息心率"),
            ("hrv_rmssd", "HRV"), ("sleep_hours", "睡眠(小时)"), ("body_battery", "身体电量"),
            ("recovery_pct", "恢复(%)"), ("recovery_level", "恢复分级"),
        ],
    },
    "injury_logs": {
        "label": "伤病记录",
        "model": models.InjuryLog,
        "date_col": "start_date",
        "dims": [
            {"key": "severity", "label": "伤病等级", "labels": _INJURY_SEVERITY_LABELS},
            {"key": "body_part", "label": "部位"},
        ],
        "measures": [],
        "details": [
            ("start_date", "起病日期"), ("end_date", "痊愈日期"), ("body_part", "部位"),
            ("side", "侧别"), ("severity", "伤病等级"), ("cause", "诱因"), ("notes", "备注"),
        ],
    },
    "fitness_snapshots": {
        "label": "体能快照",
        "model": models.FitnessSnapshot,
        "date_col": "date",
        "dims": [
            {"key": "source", "label": "来源", "labels": {"coros": "高驰"}},
            {"key": "load_comment", "label": "负荷评语"},
        ],
        "measures": [
            ("vo2max", "VO2max"), ("running_level", "跑力等级"),
            ("threshold_pace_sec", "阈值配速(秒/km)"), ("short_load", "短期负荷"),
            ("long_load", "长期负荷"), ("load_ratio", "负荷比"),
        ],
        "details": [
            ("date", "日期"), ("vo2max", "VO2max"), ("running_level", "跑力等级"),
            ("threshold_pace_sec", "阈值配速(秒/km)"), ("short_load", "短期负荷"),
            ("long_load", "长期负荷"), ("load_ratio", "负荷比"), ("load_comment", "负荷评语"),
        ],
    },
}

# 聚合标签模板：{m} 是白名单里登记的指标中文名
AGG_LABELS = {"count": "{m}计数", "sum": "总{m}", "avg": "平均{m}",
              "min": "最小{m}", "max": "最大{m}"}
_AGG_FUNCS = {"count": func.count, "sum": func.sum, "avg": func.avg,
              "min": func.min, "max": func.max}


def _compile_table(t: dict) -> None:
    """导入期编译：注册表写错（键冲突/列不存在）在 import 时就炸；
    合法维度/聚合的全部 SQL 表达式在此预构建，请求路径只查表不构建。"""
    cols = {c.name: c for c in t["model"].__table__.columns}
    if t["date_col"] not in cols:
        raise RuntimeError(f"探索表注册错误：日期列 {t['date_col']} 不存在")
    dim_keys = {d["key"] for d in t["dims"]}
    clash = dim_keys & set(GRAIN_DIMS)
    if clash:
        raise RuntimeError(f"探索表注册错误：自定义维度撞保留日期维度 {clash}")
    date_col = cols[t["date_col"]]

    # 可分组/可筛选的列表达式：自定义维度列 + 指标列 + 日期列本体 + 三个日期粒度
    # （日期列不作为分组维度暴露，但要支持按原始值筛选）
    exprs: dict = {k: cols[k] for k in dim_keys | {m for m, _ in t["measures"]}}
    exprs[t["date_col"]] = date_col
    exprs.update({name: build(date_col) for name, (_, build) in GRAIN_DIMS.items()})
    t["_exprs"] = exprs

    # 全部合法 (指标列, 聚合) 组合的预编译；("*", 非 count) 不在表内 → 自然拒绝
    agg_exprs: dict = {("*", "count"): func.count()}
    for measure, _ in t["measures"]:
        for agg, fn in _AGG_FUNCS.items():
            agg_exprs[(measure, agg)] = fn(cols[measure])
    t["_agg_exprs"] = agg_exprs
    t["_detail_cols"] = [cols[col] for col, _ in t["details"]]


for _t in TABLES.values():
    _compile_table(_t)


def _known_tables() -> str:
    return "、".join(f"{k}({v['label']})" for k, v in TABLES.items())


def _dim_options(t: dict) -> str:
    keys = "、".join(d["key"] for d in t["dims"]) or "（无自定义维度）"
    return f"{keys}；日期粒度 day/month/weekday"


def get_table(key: str) -> dict:
    t = TABLES.get(key)
    if not t:
        raise ValueError(f"未知数据表：{key}（可用：{_known_tables()}）")
    return t


def _dim_by_key(t: dict, key: str) -> dict:
    """按键取维度元数据（中文标签/值映射）；保留日期维度动态补一份。"""
    for d in t["dims"]:
        if d["key"] == key:
            return d
    if key in GRAIN_DIMS:
        label, _ = GRAIN_DIMS[key]
        return {"key": key, "label": label,
                "labels": WEEKDAY_LABELS if key == "weekday" else None}
    raise ValueError(f"未知分组维度：{key}（{t['label']}可用维度：{_dim_options(t)}）")


def _dim_expr(t: dict, key: str):
    """分组表达式：查导入期编译好的表达式表，未登记的键一律拒绝。"""
    expr = t["_exprs"].get(key)
    if expr is None:
        raise ValueError(f"未知分组维度：{key}（{t['label']}可用维度：{_dim_options(t)}）")
    return expr


def _metric_expr(t: dict, col: str, agg: str):
    """聚合表达式查表：(指标列, 聚合) 组合必须在白名单内，否则拒绝。"""
    expr = t["_agg_exprs"].get((col, agg))
    if expr is None:
        measures = "、".join(m for m, _ in t["measures"]) or "（无，仅记录数）"
        raise ValueError(f"不支持的指标聚合组合：{col}/{agg}"
                         f"（{t['label']}可用指标：{measures}；\"*\" 只配 count）")
    label = "记录数" if col == "*" else AGG_LABELS[agg].format(m=dict(t["measures"])[col])
    return expr, label


def _filter_expr(t: dict, key: str):
    """筛选表达式查表：维度列/指标列/日期粒度之外一律拒绝。"""
    expr = t["_exprs"].get(key)
    if expr is None:
        raise ValueError(f"未知筛选列：{key}"
                         f"（{t['label']}可筛选：{_dim_options(t)}，以及全部指标列）")
    return expr


def _like_contains(col_expr, value):
    """LIKE 字面量包含匹配：通配符先转义，用户输入不当模式语法解释。"""
    escaped = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return col_expr.like("%" + escaped + "%", escape="\\")


def _op_expr(op: str, col_expr, value):
    if op == "eq":
        return col_expr == value
    if op == "ne":
        return col_expr != value
    if op == "gt":
        return col_expr > value
    if op == "ge":
        return col_expr >= value
    if op == "lt":
        return col_expr < value
    if op == "le":
        return col_expr <= value
    return _like_contains(col_expr, value)  # like，文本列模糊匹配


def _base_conds(t: dict, athlete_id: int, date_from: date | None, date_to: date | None) -> list:
    """档案圈定 + 日期范围。上界用「< date_to+1天」而非「<= date_to」：
    日期列可能是 DateTime（activities.start_time），字符串比较下
    '2026-09-29 10:00' > '2026-09-29' 会把末日当天的课漏掉。"""
    date_col = t["_exprs"][t["date_col"]]
    conds = [t["model"].athlete_id == athlete_id]
    if date_from:
        conds.append(date_col >= date_from)
    if date_to:
        conds.append(date_col < date_to + timedelta(days=1))
    return conds


def _plain(value):
    """行值归一化：date/datetime 转 ISO 字符串、浮点收敛两位小数，前端直接渲染。"""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 2)
    return value


def _label_maps(t: dict, group_by: list[str]) -> dict:
    out = {}
    for key in group_by:
        labels = _dim_by_key(t, key).get("labels")
        if labels:
            out[key] = labels
    return out


def run_aggregate(db: Session, table_key: str, group_by: list[str],
                  metrics: list[tuple[str, str]], filters: list[tuple[str, str, object]],
                  date_from: date | None, date_to: date | None, limit: int) -> dict:
    """分组聚合：0-2 个维度 + 1-6 个指标，全部使用导入期预编译的表达式。

    metrics/filters 是已过 pydantic 校验的 (col, agg)/(col, op, value) 元组，
    白名单校验在这里（ValueError）。
    """
    t = get_table(table_key)
    athlete = get_default_athlete(db)
    columns: list[dict] = []
    rows: list[dict] = []
    truncated = False
    if athlete:
        conds = _base_conds(t, athlete.id, date_from, date_to)
        conds += [_op_expr(op, _filter_expr(t, col), value) for col, op, value in filters]
        group_exprs = [_dim_expr(t, key) for key in group_by]
        metric_exprs = [_metric_expr(t, col, agg) for col, agg in metrics]

        raw = db.execute(
            select(*(group_exprs + [expr for expr, _ in metric_exprs]))
            .where(*conds)
            .group_by(*group_exprs)  # 空列表 = no-op（整体合计单行）
            .order_by(metric_exprs[0][0].desc(), *group_exprs)
            .limit(limit)
        ).all()
        truncated = len(raw) == limit
        columns = [{"key": key, "label": _dim_by_key(t, key)["label"], "kind": "dim"}
                   for key in group_by]
        columns += [{"key": col + "_" + agg, "label": label, "kind": "metric"}
                    for (col, agg), (_, label) in zip(metrics, metric_exprs, strict=True)]
        metric_keys = [col + "_" + agg for col, agg in metrics]
        for record in raw:
            row = {key: _plain(record[i]) for i, key in enumerate(group_by)}
            row.update({key: _plain(record[len(group_by) + i])
                        for i, key in enumerate(metric_keys)})
            rows.append(row)
    return {"columns": columns, "rows": rows, "total": len(rows),
            "truncated": truncated, "label_maps": _label_maps(t, group_by)}


def run_detail(db: Session, table_key: str, filters: list[tuple[str, str, object]],
               date_from: date | None, date_to: date | None, limit: int) -> dict:
    """明细查询：按聚合行的维度值作 eq 筛选，回来看原始记录。"""
    t = get_table(table_key)
    athlete = get_default_athlete(db)
    columns = [{"key": k, "label": lb} for k, lb in t["details"]]
    rows: list[dict] = []
    truncated = False
    if athlete:
        conds = _base_conds(t, athlete.id, date_from, date_to)
        conds += [_op_expr(op, _filter_expr(t, col), value) for col, op, value in filters]
        raw = db.execute(
            select(*t["_detail_cols"])
            .where(*conds)
            .order_by(t["_exprs"][t["date_col"]].desc())
            .limit(limit)
        ).all()
        truncated = len(raw) == limit
        rows = [{k: _plain(record[i]) for i, (k, _) in enumerate(t["details"])} for record in raw]
    return {"columns": columns, "rows": rows, "total": len(rows), "truncated": truncated}

"""训练计划 → ICS 日历文件（可导入系统日历或订阅）。

课型中文标签统一走 services.vocab.session_label——这里绝不再内联一份课型
映射（历史教训：多处分叉导致二代课型显示成英文代码）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import models
from ..models import utcnow_naive
from ..services.vocab import session_label


def _fmt_dt(d: datetime) -> str:
    """ICS 本地浮动时间（不带 Z，跟随用户日历的本地时区语义）。"""
    return d.strftime("%Y%m%dT%H%M%S")


def _escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\n", "\\n")


def _event(wo: models.PlanWorkout) -> str:
    start_h, start_m = "19", "00"
    if wo.start_time:
        parts = wo.start_time.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            start_h, start_m = parts[0][:2], parts[1][:2]
    start = datetime(wo.date.year, wo.date.month, wo.date.day,
                     int(start_h), int(start_m))
    minutes = int(wo.duration_min) if wo.duration_min else 60
    end = start + timedelta(minutes=max(15, minutes))

    parts = [session_label(wo.session_type), wo.title]
    if wo.distance_km:
        parts.append(f"{wo.distance_km:g}km")
    summary = " · ".join(dict.fromkeys(p for p in parts if p))

    desc_lines = []
    if wo.duration_min:
        desc_lines.append(f"时长约 {round(wo.duration_min)} 分钟")
    desc_lines.append(f"状态：{wo.status}")
    if wo.description:
        desc_lines.append(wo.description)

    return "\r\n".join([
        "BEGIN:VEVENT",
        f"UID:sport-platform-{wo.id}@local",
        f"DTSTAMP:{_fmt_dt(utcnow_naive())}",
        f"DTSTART:{_fmt_dt(start)}",
        f"DTEND:{_fmt_dt(end)}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(chr(10).join(desc_lines))}",
        "CATEGORIES:TRAINING",
        "END:VEVENT",
    ])


def build_plan_ics(plan: models.TrainingPlan) -> str:
    """当前计划 → 完整 VCALENDAR 文本（含所有课次事件）。"""
    events = []
    for week in plan.weeks:
        for wo in week.workouts:
            if wo.status in ("planned", "synced"):
                events.append(_event(wo))
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//sport-platform//training plan//CN",
        "CALSCALE:GREGORIAN",
        *events,
        "END:VCALENDAR",
        "",
    ])

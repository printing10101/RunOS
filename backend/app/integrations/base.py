"""平台适配器基类与通用异常。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class IntegrationError(Exception):
    """对接配置/调用失败的统一异常，message 面向用户展示。"""


def maybe_int(value) -> int | None:
    """宽松转 int：None/非法字符串返回 None；小数四舍五入。"""
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def maybe_float(value) -> float | None:
    """宽松转 float：None/非法字符串返回 None。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class NormalizedActivity:
    """各平台原始数据归一化后的活动。"""
    external_id: str
    sport: str                 # run/ride/swim/strength/walk/other
    title: str
    start_time: datetime
    duration_sec: int
    distance_m: float = 0
    avg_hr: int | None = None
    max_hr: int | None = None
    elevation_m: float = 0
    avg_cadence: float | None = None
    avg_power: float | None = None
    calories: int | None = None
    temp_c: float | None = None
    weather: str | None = None
    te_aerobic: float | None = None    # 有氧训练效果 0-5
    te_anaerobic: float | None = None  # 无氧训练效果 0-5
    dynamics: dict = field(default_factory=dict)  # 跑步动态/专项扩展（步幅/振幅/触地/划次等）
    raw: dict = field(default_factory=dict)


class PlatformAdapter(ABC):
    """所有运动平台适配器的接口。"""

    platform: str = "base"

    def __init__(self, credentials: dict):
        self.credentials = credentials or {}

    @abstractmethod
    def check_config(self) -> None:
        """校验凭据是否齐全，不满足时抛 IntegrationError（含申请指引）。"""

    @abstractmethod
    def fetch_activities(self, since: datetime) -> list[NormalizedActivity]:
        """拉取 since 之后的活动。"""

    @abstractmethod
    def push_workout(self, workout_name: str, structured_steps: list[dict],
                     sport: str = "run") -> str:
        """下发结构化训练到平台训练库，返回平台 workout id。"""

    def status(self) -> dict:
        return {"platform": self.platform, "mode": "official", "ok": True}

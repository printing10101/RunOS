from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .services.vdot import GOAL_RACE_TYPES

# 比赛项目词汇单源在 services/vdot.py（RACE_DISTANCES），这里只做类型化包装；
# 加新项目改那边即可，目标 / AI 建计划 / 比赛成绩三处入口同时生效。
RaceType = Literal[*GOAL_RACE_TYPES]              # type: ignore[valid-type]
RaceTypeOrOther = Literal[*GOAL_RACE_TYPES, "other"]  # type: ignore[valid-type]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AthleteIn(BaseModel):
    """跑者档案写入。

    **留空（null / 不传）= 交给系统按本人数据自动解析**，而不是回落到某个写死的常量。
    身份事实（sex / birth_year / height_cm / weight_kg）无法从训练数据推导，
    首次建档必须由用户提供；可解析状态（resting_hr / max_hr / hrv_baseline /
    training_age_years）留空即由 services.profile 的解析链给出，并随数据实时更新。

    更新语义：路由层用 ``exclude_unset=True``，未传的字段保持库内现值不动。
    """

    name: str | None = Field(default=None, max_length=64)
    sex: Literal["male", "female"] | None = None
    birth_year: int | None = Field(default=None, ge=1930, le=2100)
    height_cm: float | None = Field(default=None, gt=0, description="0 会触发 BMI 除零，压到入口拦截")
    weight_kg: float | None = Field(default=None, gt=0)
    resting_hr: int | None = Field(default=None, ge=30, le=120)
    max_hr: int | None = Field(default=None, ge=100, le=230)
    hrv_baseline: int | None = Field(default=None, ge=10, le=200)
    training_age_years: float | None = Field(default=None, ge=0, le=80)

    @model_validator(mode="after")
    def _hr_relation(self):
        if (self.resting_hr is not None and self.max_hr is not None
                and self.resting_hr >= self.max_hr):
            raise ValueError("静息心率必须小于最大心率")
        return self


class AthleteAutoIn(BaseModel):
    """单个画像字段的「自动推算」开关。关闭=钉死当前值，打开=下次读取即跟随数据。"""

    field: Literal["max_hr", "resting_hr", "hrv_baseline", "weight_kg", "training_age_years"]
    auto: bool


class GoalIn(BaseModel):
    race_type: RaceType
    target_time_sec: int | None = None
    target_label: str = ""
    target_date: date | None = None


class WeeklySlotIn(BaseModel):
    weekday: int = 0
    start_time: str = "19:00"
    duration_minutes: int = 60
    kind: Literal["available", "busy"] = "available"
    label: str = ""


class ConnectIn(BaseModel):
    platform: Literal["coros", "garmin", "strava"]
    mode: Literal["official"] = "official"
    credentials: dict = {}


class SyncIn(BaseModel):
    since_days: int = 365  # 默认拉取最近一年（后端按 60 天分段滚动，上限 480 天）
    # 单次同步最多补拉多少条活动详情（详情含步频/功率/训练效果，列表接口没有）
    detail_limit: int = 10
    # 是否顺带回填历史活动的缺失详情指标（用于修复早期同步的空列）
    backfill_detail: bool = False


class AutoSyncIn(BaseModel):
    """自动同步设置（None = 不修改该项）。间隔范围 4-1440 分钟。"""
    enabled: bool | None = None
    minutes: int | None = None


class StrengthTestIn(BaseModel):
    date: date
    exercise: str
    best_weight_kg: float = Field(gt=0)
    reps: int = Field(default=1, ge=1, le=50, description="reps<1 会让 1RM 折算静默降级")
    bodyweight_kg: float | None = Field(default=None, gt=0)
    notes: str = ""


class DietLogIn(BaseModel):
    date: date
    meal: Literal["breakfast", "lunch", "dinner", "snack"]
    description: str = ""
    kcal: float = Field(default=0, ge=0)
    protein_g: float = Field(default=0, ge=0)
    carb_g: float = Field(default=0, ge=0)
    fat_g: float = Field(default=0, ge=0)


class PlanGenerateIn(BaseModel):
    goal_id: int
    start_date: date | None = None
    weekly_km_peak: float | None = None  # 留空则按可用时间与水平自动推算
    plan_name: str = ""


class AiChatIn(BaseModel):
    messages: list[dict] = []  # [{role: user|assistant, content: str}]
    # 会话模式：携带 conversation_id + message 时，历史由服务端从库内加载
    # 并持久化本轮问答；只带 messages 时为无状态调用（不落库）。
    conversation_id: int | None = None
    message: str | None = Field(None, max_length=4000, description="本轮用户输入")


class AiModelSelectIn(BaseModel):
    """切换本地模型：model 为 /ai/models 列表里的 id（在线模型或本地 GGUF）。"""
    model: str = Field(min_length=1, max_length=128)


class AiProposalApplyIn(BaseModel):
    kind: Literal["move_workout", "quality_adjustment", "easy_replacement",
                  "profile_update", "goal_update", "add_workout", "skip_workout"]
    workout_id: int | None = None  # 课表类提案必填；profile_update 不需要
    target_weekday: int | None = None
    target_reps: int | None = None
    changes: dict | None = None  # profile_update：{字段: 新值}，服务端重新校验
    # 字段名不能叫 date：from __future__ annotations 下，带默认值的 date 注解
    # 会被类命名空间里的默认值遮蔽，求值即崩（CheckinIn.date 无默认值故无此问题）。
    target_date: date | None = None  # add_workout：加课日期


class AiPlanFromTextIn(BaseModel):
    text: str = Field(max_length=2000, description="超长文本会打爆提取 prompt（system 嵌入侧也截 500）")


class AiConversationCreateIn(BaseModel):
    title: str = Field(default="新对话", max_length=64)


class CoachNoteCreateIn(BaseModel):
    """用户手动添加教练长期记忆（AI 对话内自动记录走 remember_user_note 工具）。"""
    content: str = Field(min_length=2, max_length=200)
    category: str = Field(default="other")


class AiWorkoutCommentIn(BaseModel):
    """训练后 AI 点评：默认幂等（已有点评直接返回），force=True 重新生成覆盖。"""
    workout_id: int
    force: bool = False


class AiPlanConfirmIn(BaseModel):
    race_type: RaceType
    target_time_sec: int | None = None
    target_date: date | None = None
    target_label: str = ""
    weekly_km_peak: float | None = None
    start_date: date | None = None


class PushIn(BaseModel):
    # 默认只含支持下发写入的平台；高驰 MCP 未开放训练写入，
    # 仍接受显式传参以便未来开放
    platforms: list[Literal["coros", "garmin", "strava"]] = ["garmin", "strava"]


class ActivityIn(BaseModel):
    """录入/导入单个活动。distance_m/duration_sec 提供 map 边界校验，负数会被 422 拦截，
    避免负配速/负卡路里/负训练分写入。"""
    sport: Literal["run", "ride", "swim", "strength", "walk", "other"] = "run"
    title: str = ""
    start_time: datetime
    duration_sec: int = Field(..., ge=0)
    distance_m: float = Field(0, ge=0)
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_cadence: float | None = None
    elevation_m: float = Field(0, ge=0)
    calories: int | None = None
    avg_power: float | None = None
    temp_c: float | None = None
    weather: str | None = None
    rpe: int | None = Field(None, ge=0, le=10)
    gear_id: int | None = None  # 关联装备（跑鞋），计入其累计里程


class ActivityUpdate(BaseModel):
    """修正已有活动：全部字段可选，配合 PUT exclude_unset 实现「只改传入的字段」。
    校验规则与 ActivityIn 一致（负数被 422 拦截）。"""
    sport: Literal["run", "ride", "swim", "strength", "walk", "other"] | None = None
    title: str | None = None
    start_time: datetime | None = None
    duration_sec: int | None = Field(None, ge=0)
    distance_m: float | None = Field(None, ge=0)
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_cadence: float | None = None
    elevation_m: float | None = Field(None, ge=0)
    calories: int | None = None
    avg_power: float | None = None
    temp_c: float | None = None
    weather: str | None = None
    rpe: int | None = Field(None, ge=0, le=10)
    gear_id: int | None = None


class BodyMetricIn(BaseModel):
    """每日身体数据手动录入/修正（按日期 upsert，字段可缺省）。"""
    date: date
    weight_kg: float | None = Field(default=None, ge=20, le=400)
    resting_hr: int | None = Field(default=None, ge=25, le=150)
    hrv_rmssd: float | None = Field(default=None, ge=0, le=400)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    sleep_score: int | None = Field(default=None, ge=0, le=100)
    body_fat_pct: float | None = Field(default=None, ge=0, le=70)
    spo2: float | None = Field(default=None, ge=50, le=100)
    resp_rate: float | None = Field(default=None, ge=4, le=40)
    stress: int | None = Field(default=None, ge=0, le=100)
    body_battery: int | None = Field(default=None, ge=0, le=100)


class PlanWorkoutCompleteIn(BaseModel):
    activity_id: int | None = None
    completed: bool = True  # False = 取消完成，退回 planned


class GearIn(BaseModel):
    """装备录入/更新（跑鞋等）。retire_km 留空用默认 600（跑鞋常规更换里程）。"""
    name: str = Field(max_length=64, description="与 models.Gear.name 的 String(64) 对齐")
    kind: Literal["shoe", "watch", "other"] = "shoe"
    brand: str = ""
    start_date: date | None = None
    initial_km: float = Field(default=0, ge=0)
    retire_km: float = Field(default=600, gt=0)
    notes: str = ""


class GearAssignIn(BaseModel):
    gear_id: int | None = None  # None 表示取消关联


class RaceResultIn(BaseModel):
    """比赛实测成绩录入：成绩预测校准闭环的「真值」。"""
    date: date
    race_name: str = ""
    race_type: RaceTypeOrOther = "other"
    distance_m: float
    time_sec: int
    avg_hr: int | None = None
    is_official: bool = True
    notes: str = ""


class CheckinIn(BaseModel):
    """每日主观打卡（按日期 upsert）。睡眠质量/精力/动力 1-5，酸痛 0-5。"""
    date: date
    sleep_quality: int = 3
    muscle_soreness: int = 0
    energy_level: int = 3
    motivation: int = 3
    pain_area: str = ""
    note: str = ""


class DietEstimateIn(BaseModel):
    """食物描述 → 本地模型营养素估算。"""
    description: str


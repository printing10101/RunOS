"""全部 ORM 模型。面向个人的单用户/多档案权设计，暂不做鉴权体系。"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow_naive() -> datetime:
    """朴素 UTC 时间戳（无 tzinfo），用作 created_at 的默认值。

    不用 ``datetime.utcnow()``：自 Python 3.12 起被弃用。

    返回 naive 而不是 aware：SQLite 的 DateTime 列实际存字符串，历史数据是
    ``YYYY-MM-DD HH:MM:SS`` 朴素格式；aware 新写入会带 ``+00:00`` 后缀，
    与既有行格式不一致，读取解析可能出错。这里显式构造 aware 再剥离 tzinfo。

    时间基准约定：本字段存 UTC；而面向用户展示的 ``last_sync_at`` 等存本地
    时间（``datetime.now()``）。两者基准不同，不要直接相互比较。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Athlete(Base):
    __tablename__ = "athletes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # ---- 身份事实：只能由用户提供，系统不得代为编造 ----
    sex: Mapped[str] = mapped_column(String(8))                  # male / female
    birth_year: Mapped[int] = mapped_column()                    # 年龄是一切生理推定的输入
    height_cm: Mapped[float] = mapped_column(Float)
    # ---- 可解析状态：写入前一律先过 services.profile 的解析链 ----
    # 历史坑：这些列曾带 default=65/55/190/55/2.0，新建档案即被写入一组
    # 与本人无关的常量，且之后永不更新，导致所有用户拿到同一套画像
    # （心率区间、负荷、天赋评估全部失真）。现改为「无默认值」：
    # 任何创建路径都必须显式给值，取值来源与依据记录在 profile_meta。
    weight_kg: Mapped[float] = mapped_column(Float)
    resting_hr: Mapped[int] = mapped_column(Integer)
    max_hr: Mapped[int] = mapped_column(Integer)
    hrv_baseline: Mapped[int] = mapped_column(Integer)           # rmssd ms
    training_age_years: Mapped[float] = mapped_column(Float)     # 系统训练年限
    # 每个可解析字段的来源溯源：{field: {source, basis, confidence, at, locked}}
    profile_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    goals: Mapped[list[Goal]] = relationship(back_populates="athlete", order_by="Goal.id")
    schedule_slots: Mapped[list[WeeklySlot]] = relationship(back_populates="athlete", order_by="WeeklySlot.id")
    connections: Mapped[list[PlatformConnection]] = relationship(back_populates="athlete")


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    race_type: Mapped[str] = mapped_column(String(16))  # 5k / 10k / hm / marathon
    target_time_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_label: Mapped[str] = mapped_column(String(64), default="")  # 如 "全马破三"
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="primary")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / achieved / paused
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    athlete: Mapped[Athlete] = relationship(back_populates="goals")


class WeeklySlot(Base):
    """每周固定日程（课表/上班/家庭），kind=available 的时段才可安排训练。"""

    __tablename__ = "weekly_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    weekday: Mapped[int] = mapped_column(Integer)  # 0=周一 ... 6=周日
    start_time: Mapped[str] = mapped_column(String(5), default="19:00")  # HH:MM
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    kind: Mapped[str] = mapped_column(String(16), default="available")  # available / busy
    label: Mapped[str] = mapped_column(String(64), default="")  # 如 "晚自习" "工作时间"

    athlete: Mapped[Athlete] = relationship(back_populates="schedule_slots")


class PlatformConnection(Base):
    __tablename__ = "platform_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    platform: Mapped[str] = mapped_column(String(16))  # coros / garmin
    mode: Mapped[str] = mapped_column(String(16), default="official")
    status: Mapped[str] = mapped_column(String(16), default="connected")
    ext_athlete_id: Mapped[str] = mapped_column(String(128), default="")
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)  # token 等，本地自用明文存储
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    athlete: Mapped[Athlete] = relationship(back_populates="connections")


class Activity(Base):
    __tablename__ = "activities"
    # 平台活动去重唯一索引（partial：手动录入 external_id 为空串，不参与去重）。
    # _insert_activity 的 IntegrityError 兜底依赖它；老库由 db._auto_migrate 补建。
    __table_args__ = (
        Index(
            "ux_activities_platform_external",
            "platform",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL AND external_id != ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(16), default="manual")
    sport: Mapped[str] = mapped_column(String(16), default="run")  # run/ride/swim/strength/walk/other
    title: Mapped[str] = mapped_column(String(128), default="")
    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    distance_m: Mapped[float] = mapped_column(Float, default=0)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_m: Mapped[float] = mapped_column(Float, default=0)
    avg_cadence: Mapped[float | None] = mapped_column(Float, nullable=True)  # 步频 spm
    avg_power: Mapped[float | None] = mapped_column(Float, nullable=True)  # 平均功率 W
    rpe: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 主观强度 1-10
    training_load: Mapped[float] = mapped_column(Float, default=0)  # TRIMP 或 duration*RPE
    effort_score: Mapped[float] = mapped_column(Float, default=0)  # 相对强度分
    calories: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 千卡
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)  # 气温 ℃
    weather: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 天气
    te_aerobic: Mapped[float | None] = mapped_column(Float, nullable=True)  # 有氧训练效果 0-5
    te_anaerobic: Mapped[float | None] = mapped_column(Float, nullable=True)  # 无氧训练效果 0-5
    dynamics: Mapped[dict] = mapped_column(JSON, default=dict)  # 跑步动态/专项扩展字段
    gear_id: Mapped[int | None] = mapped_column(ForeignKey("gears.id"), nullable=True)  # 使用的装备（跑鞋等）
    gear: Mapped[Gear | None] = relationship()
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Gear(Base):
    """装备/跑鞋跟踪：累计里程 + 寿命提醒。"""

    __tablename__ = "gears"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="shoe")  # shoe / watch / other
    brand: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[date] = mapped_column(Date, default=date.today)  # 启用日期
    initial_km: Mapped[float] = mapped_column(Float, default=0)  # 启用时已有里程（二手/历史）
    retire_km: Mapped[float] = mapped_column(Float, default=600)  # 建议退役里程（跑鞋 600-800km）
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / retired
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class RaceResult(Base):
    """比赛实测成绩：成绩预测的「真值锚点」，用于校准 Riegel 指数与偏差复盘。"""

    __tablename__ = "race_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    race_name: Mapped[str] = mapped_column(String(128), default="")
    race_type: Mapped[str] = mapped_column(String(16), default="other")  # 5k/10k/hm/marathon/other
    distance_m: Mapped[float] = mapped_column(Float)
    time_sec: Mapped[int] = mapped_column(Integer)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_official: Mapped[bool] = mapped_column(Boolean, default=True)  # 正式比赛 vs 自测
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class RacePrediction(Base):
    """赛前预测快照：比赛日前逐日留档当前预测，赛后与实测对比复盘偏差。

    兑现 README「赛前预测 vs 实际偏差复盘」：此前只有实测成绩表，预测一闪而过，
    复盘无从谈起。幂等键 (athlete_id, race_date, distance_m, predicted_on)——
    同一天多次预测只留一条，快照由预测装配入口按需写入。
    """

    __tablename__ = "race_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    race_date: Mapped[date] = mapped_column(Date, index=True)
    race_type: Mapped[str] = mapped_column(String(16), default="other")
    race_name: Mapped[str] = mapped_column(String(128), default="")
    distance_m: Mapped[float] = mapped_column(Float)
    predicted_sec: Mapped[int] = mapped_column(Integer)
    predicted_on: Mapped[date] = mapped_column(Date, index=True)  # 快照日（哪天的预测）
    actual_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 赛后回填
    delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True)   # (实测-预测)/预测*100
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class DailyCheckin(Base):
    """每日主观感受打卡（晨间问卷）：睡眠质量/酸痛/精力/动力，
    AI 自适应调课的主观输入通道。每档案每人每天一条（按日期 upsert）。"""

    __tablename__ = "daily_checkins"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    sleep_quality: Mapped[int] = mapped_column(Integer, default=3)  # 1-5 昨晚睡眠质量
    muscle_soreness: Mapped[int] = mapped_column(Integer, default=0)  # 0-5 肌肉酸痛程度
    energy_level: Mapped[int] = mapped_column(Integer, default=3)  # 1-5 精力/体力感受
    motivation: Mapped[int] = mapped_column(Integer, default=3)  # 1-5 训练意愿
    pain_area: Mapped[str] = mapped_column(String(64), default="")  # 疼痛/不适部位（可空）
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class BodyMetric(Base):
    """每日身体状态：体重/静息心率/HRV/睡眠/体脂/血氧/呼吸率/压力/身体电量。"""

    __tablename__ = "body_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    resting_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hrv_rmssd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 睡眠分数 0-100
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)  # 血氧 %
    resp_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 呼吸率 次/分
    stress: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 压力分数 0-100
    body_battery: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 身体电量 0-100
    # 官方恢复状态（高驰 queryRecoveryStatus 当期快照 → 当日行）：
    # recovery_pct 恢复百分比 / recovery_level 官方分级文案 / recovery_hours 预计完全恢复小时数
    recovery_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recovery_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FitnessSnapshot(Base):
    """高驰官方体能快照（queryFitnessAssessmentOverview / queryTrainingLoadAssessment）。

    按 (athlete_id, date) 一天一行：负荷历史逐日回填，当期体能评估落到今天。
    字段缺失一律 None 不编造；race_predictions 存各距离官方预测秒数。
    """

    __tablename__ = "fitness_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    # ---- queryFitnessAssessmentOverview（当期评估）----
    vo2max: Mapped[int | None] = mapped_column(Integer, nullable=True)       # 官方 VO2max
    running_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold_pace_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    race_predictions: Mapped[dict] = mapped_column(JSON, default=dict)       # {5k: sec, ...}
    # ---- queryTrainingLoadAssessment（逐日负荷比）----
    short_load: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 短期负荷
    long_load: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 长期负荷
    load_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)   # 负荷比
    load_comment: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Optimized 等
    # 快照来源平台（恢复库 09-21 已有此列，NOT NULL）
    source: Mapped[str] = mapped_column(String(32), default="coros")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    __table_args__ = (
        UniqueConstraint("athlete_id", "date", name="ux_fitness_snapshot_athlete_date"),
    )


class StrengthTest(Base):
    __tablename__ = "strength_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    exercise: Mapped[str] = mapped_column(String(32))  # squat/bench/deadlift/ohp/row/pullup/hip_thrust/core
    best_weight_kg: Mapped[float] = mapped_column(Float)
    reps: Mapped[int] = mapped_column(Integer, default=1)  # >1 时折算 1RM
    bodyweight_kg: Mapped[float] = mapped_column(Float, default=65)
    notes: Mapped[str] = mapped_column(Text, default="")


class DietLog(Base):
    __tablename__ = "diet_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    meal: Mapped[str] = mapped_column(String(16))  # breakfast/lunch/dinner/snack
    description: Mapped[str] = mapped_column(String(255), default="")
    kcal: Mapped[float] = mapped_column(Float, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0)
    carb_g: Mapped[float] = mapped_column(Float, default=0)
    fat_g: Mapped[float] = mapped_column(Float, default=0)


class TrainingPlan(Base):
    __tablename__ = "training_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    race_type: Mapped[str] = mapped_column(String(16))
    target_time_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date] = mapped_column(Date)
    race_date: Mapped[date] = mapped_column(Date)
    weekly_km_peak: Mapped[float] = mapped_column(Float, default=60)
    feasibility: Mapped[dict] = mapped_column(JSON, default=dict)  # 可行性评估
    # 计划来源：ai=AI 生成 / method=方法库体验周 / coros=高驰官方镜像 / manual
    source: Mapped[str] = mapped_column(String(16), default="ai")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    weeks: Mapped[list[PlanWeek]] = relationship(back_populates="plan", order_by="PlanWeek.week_index",
                                                   cascade="all, delete-orphan")


class PlanWeek(Base):
    __tablename__ = "plan_weeks"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("training_plans.id"), index=True)
    week_index: Mapped[int] = mapped_column(Integer)  # 从 1 开始
    start_date: Mapped[date] = mapped_column(Date)
    phase: Mapped[str] = mapped_column(String(16))  # base/build/peak/taper
    phase_note: Mapped[str] = mapped_column(String(255), default="")
    target_km: Mapped[float] = mapped_column(Float, default=0)
    focus: Mapped[str] = mapped_column(String(128), default="")

    plan: Mapped[TrainingPlan] = relationship(back_populates="weeks")
    workouts: Mapped[list[PlanWorkout]] = relationship(back_populates="week", order_by="PlanWorkout.date",
                                                         cascade="all, delete-orphan")


class PlanWorkout(Base):
    __tablename__ = "plan_workouts"
    # 首页「下一节课」、执行率、饮食日型等都按 (athlete_id, date) 范围查
    __table_args__ = (
        Index("ix_plan_workouts_athlete_date", "athlete_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("plan_weeks.id"), index=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[str] = mapped_column(String(5), default="19:00")
    session_type: Mapped[str] = mapped_column(String(16))  # easy/tempo/interval/long/strength/core/rest/cross
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    distance_km: Mapped[float] = mapped_column(Float, default=0)
    duration_min: Mapped[float] = mapped_column(Float, default=0)
    structured: Mapped[list] = mapped_column(JSON, default=list)  # 结构化步骤（可下发手表）
    diet_tip: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned/synced/completed/skipped
    # 完成方式：manual=手动标记 / linked=关联活动（completed_activity_id 非空时通常为 linked）
    completed_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pushed_platforms: Mapped[list] = mapped_column(JSON, default=list)
    external_workout_id: Mapped[str] = mapped_column(String(128), default="")
    completed_activity_id: Mapped[int | None] = mapped_column(ForeignKey("activities.id"), nullable=True)
    # 训练后 AI 点评（services/coach_comment.py 生成；老库由 _MIGRATION_DDL 补列）
    coach_comment: Mapped[str] = mapped_column(Text, default="")
    coach_comment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    week: Mapped[PlanWeek] = relationship(back_populates="workouts")


class AiConversation(Base):
    """AI 教练对话会话（聊天记录持久化）。"""

    __tablename__ = "ai_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    title: Mapped[str] = mapped_column(String(64), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive,
                                                 onupdate=utcnow_naive)

    messages: Mapped[list[AiMessage]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="AiMessage.id")


class AiMessage(Base):
    """会话内单条消息。助手消息冗余存工具调用标签与提案卡片，恢复会话时按原样回放。"""

    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[list] = mapped_column(JSON, default=list)        # [{label, ok}]
    proposals: Mapped[list] = mapped_column(JSON, default=list)    # 提案卡片（含 apply 载荷）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    conversation: Mapped[AiConversation] = relationship(back_populates="messages")


class AppSetting(Base):
    """运行时键值设置（自动同步开关/间隔等），与 .env 的初始默认值解耦。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive,
                                                 onupdate=utcnow_naive)


class TrainingMethod(Base):
    """训练方法/体系知识库（全局，不绑定某个运动员）。

    面向「AI 训练与选择」：把全网调研来的训练体系拆成机器可读的结构化字段
    （强度分布/跑量门槛/适配水平/目标项目/周期阶段/证据等级/亚洲适配依据），
    AI 教练据此检索与打分，而不是靠大模型记忆编造训练法。

    证据等级约定（与 method_evidence.level 一致）：
      A = 有随机对照试验或系统综述/meta 分析支持
      B = 有观察性研究/精英群体训练日志统计支持
      C = 有公开的一手实践记录（教练著作/运动员自述/队伍公开资料）
      D = 仅二手媒体转述或方法论主张，未见可核实的原始证据
    """

    __tablename__ = "training_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    name_ja: Mapped[str] = mapped_column(String(128), default="")
    name_en: Mapped[str] = mapped_column(String(128), default="")
    origin: Mapped[str] = mapped_column(String(16), default="global")  # japan/asia/western/global
    category: Mapped[str] = mapped_column(String(16), default="system")  # system=体系 / session=单课型
    summary: Mapped[str] = mapped_column(Text, default="")
    principles: Mapped[list] = mapped_column(JSON, default=list)      # 核心原则条目
    # 强度分布（占训练次数或时间的百分比），三者之和约 100
    intensity_distribution: Mapped[dict] = mapped_column(JSON, default=dict)  # {low, moderate, high}
    weekly_km_range: Mapped[dict] = mapped_column(JSON, default=dict)         # {min, max}
    sessions_per_week: Mapped[dict] = mapped_column(JSON, default=dict)       # {min, max}
    quality_days_per_week: Mapped[int] = mapped_column(Integer, default=2)
    level: Mapped[str] = mapped_column(String(16), default="intermediate")    # beginner/intermediate/advanced/elite
    target_races: Mapped[list] = mapped_column(JSON, default=list)   # 5k/10k/hm/marathon/ultra
    phases: Mapped[list] = mapped_column(JSON, default=list)         # base/build/peak/taper
    pros: Mapped[str] = mapped_column(Text, default="")
    cons: Mapped[str] = mapped_column(Text, default="")
    cautions: Mapped[str] = mapped_column(Text, default="")          # 硬约束/禁忌
    key_figures: Mapped[list] = mapped_column(JSON, default=list)    # 代表人物/教练
    tags: Mapped[list] = mapped_column(JSON, default=list)
    evidence_level: Mapped[str] = mapped_column(String(2), default="C")
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    # 亚洲/东亚跑者适配度（0-100）与依据：衡量生活方式、训练文化、气候的适配，
    # 不是生理差异；依据写在 asian_fit_basis 里。
    asian_fit: Mapped[int] = mapped_column(Integer, default=50)
    asian_fit_basis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    workouts: Mapped[list[WorkoutTemplate]] = relationship(
        back_populates="method", cascade="all, delete-orphan")
    evidences: Mapped[list[MethodEvidence]] = relationship(
        back_populates="method", cascade="all, delete-orphan")
    fit_rules: Mapped[list[MethodFitRule]] = relationship(
        back_populates="method", cascade="all, delete-orphan")


class WorkoutTemplate(Base):
    """课表模板：某个体系下可直接落到日历上的一节具体课。

    structure 复用 planner 的结构化步骤格式，但强度用「配速区 key」占位
    （E/M/T/I/R 或 hr/rpe），生成时由引擎按当前 VDOT 解析成真实数值，
    避免把配速写死在知识库里导致能力变化后失效。
    """

    __tablename__ = "workout_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("training_methods.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    name_ja: Mapped[str] = mapped_column(String(128), default="")
    session_type: Mapped[str] = mapped_column(String(16), default="quality")  # easy/tempo/interval/long/recovery/fartlek/hill/race/strength
    purpose: Mapped[str] = mapped_column(Text, default="")     # 练什么能力
    intensity_anchor: Mapped[dict] = mapped_column(JSON, default=dict)  # {type: pace_zone|hr_pct|rpe, ...}
    structure: Mapped[list] = mapped_column(JSON, default=list)         # 结构化步骤
    distance_km_range: Mapped[dict] = mapped_column(JSON, default=dict)  # {min, max}
    duration_min_range: Mapped[dict] = mapped_column(JSON, default=dict)
    weekly_km_min: Mapped[float] = mapped_column(Float, default=0)     # 建议的最低周跑量门槛
    phases: Mapped[list] = mapped_column(JSON, default=list)
    frequency_hint: Mapped[str] = mapped_column(String(128), default="")
    progression: Mapped[str] = mapped_column(Text, default="")  # 进阶/退阶做法
    cautions: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    method: Mapped[TrainingMethod] = relationship(back_populates="workouts")


class MethodEvidence(Base):
    """方法库的证据/出处：一条来源一行，保证「每个方法都能查到出处」。"""

    __tablename__ = "method_evidences"

    id: Mapped[int] = mapped_column(primary_key=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("training_methods.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(512), default="")
    source_type: Mapped[str] = mapped_column(String(24), default="media")  # rct/review/observational/book/elite_practice/media
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(2), default="C")   # A/B/C/D
    note: Mapped[str] = mapped_column(Text, default="")

    method: Mapped[TrainingMethod] = relationship(back_populates="evidences")


class MethodFitRule(Base):
    """适配规则：把「什么样的跑者适合这个方法」写成可评分的条件。

    服务层按跑者画像（周跑量/水平/目标/每周可练天数）逐条匹配，命中加分，
    让推荐结果可解释（返回命中了哪条规则），而非黑箱排序。
    """

    __tablename__ = "method_fit_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("training_methods.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    # condition 支持键：weekly_km_min/weekly_km_max/days_min/days_max/
    #                   level_in/race_in/phase_in/training_age_min/injury_sensitive
    weight: Mapped[float] = mapped_column(Float, default=10.0)
    reason: Mapped[str] = mapped_column(String(255), default="")

    method: Mapped[TrainingMethod] = relationship(back_populates="fit_rules")


class TrainingPrinciple(Base):
    """训练原理库：AI 定制计划时的「为什么」层。

    每条原理 = 机制说明 + 可执行规则 + 本平台引擎已落地的位置，
    让排课决策可以回溯到原理出处，而不是一句「经验之谈」。
    出处存 knowledge_sources（ref_type='principle'）。
    """

    __tablename__ = "training_principles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str] = mapped_column(String(128), default="")
    # periodization/intensity/load/recovery/specificity/strength/nutrition/heat/technique/psychology
    category: Mapped[str] = mapped_column(String(24), default="load")
    summary: Mapped[str] = mapped_column(Text, default="")
    mechanism: Mapped[str] = mapped_column(Text, default="")       # 生理机制
    practical_rules: Mapped[list] = mapped_column(JSON, default=list)  # 可执行规则（数字口径）
    platform_usage: Mapped[str] = mapped_column(Text, default="")  # 本平台引擎已应用的位置
    evidence_level: Mapped[str] = mapped_column(String(2), default="C")
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class PlanTemplate(Base):
    """参考训练计划库：全球知名出版计划的结构化拆解。

    不逐日复刻 18 周（那属于版权内容且无必要），而是拆出可执行的结构骨架：
    周模板（周几→课型）、长距离递进、关键课、减量方案与适配条件。
    AI 定制计划时以此为「有迹可循」的骨架参照，再按 VDOT/画像个性化。
    出处存 knowledge_sources（ref_type='plan'）。
    """

    __tablename__ = "plan_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_zh: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str] = mapped_column(String(128), default="")
    author: Mapped[str] = mapped_column(String(128), default="")
    target_race: Mapped[str] = mapped_column(String(16), default="marathon")  # 5k/10k/hm/marathon/any
    level: Mapped[str] = mapped_column(String(16), default="beginner")
    weeks: Mapped[int] = mapped_column(Integer, default=16)
    weekly_km_range: Mapped[dict] = mapped_column(JSON, default=dict)   # {min, max} 峰值周跑量
    days_per_week: Mapped[dict] = mapped_column(JSON, default=dict)     # {min, max}
    phases: Mapped[list] = mapped_column(JSON, default=list)
    week_template: Mapped[dict] = mapped_column(JSON, default=dict)     # {"mon": "easy", ...} 课型 key
    long_run_progression: Mapped[list] = mapped_column(JSON, default=list)  # [{week, km}]
    key_workouts: Mapped[list] = mapped_column(JSON, default=list)      # 关键课说明
    taper_plan: Mapped[str] = mapped_column(Text, default="")
    fit_condition: Mapped[dict] = mapped_column(JSON, default=dict)     # 与 method_fit_rules 同口径
    fit_notes: Mapped[str] = mapped_column(Text, default="")
    pros: Mapped[str] = mapped_column(Text, default="")
    cons: Mapped[str] = mapped_column(Text, default="")
    cautions: Mapped[str] = mapped_column(Text, default="")
    evidence_level: Mapped[str] = mapped_column(String(2), default="C")
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class KnowledgeSource(Base):
    """通用出处表：training_principles / plan_templates 的证据与来源。

    （training_methods 用自己的 method_evidences 表，是历史原因；新知识类型统一走本表。）
    """

    __tablename__ = "knowledge_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref_type: Mapped[str] = mapped_column(String(16), index=True)   # principle / plan
    ref_code: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(512), default="")
    source_type: Mapped[str] = mapped_column(String(24), default="media")  # rct/meta/observational/book/elite_practice/media
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(2), default="C")
    note: Mapped[str] = mapped_column(Text, default="")


class Assessment(Base):
    """综合评估快照（历史留档）。"""

    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    total_score: Mapped[float] = mapped_column(Float, default=0)
    grade: Mapped[str] = mapped_column(String(8), default="")  # S/A/B/C/D
    percentile: Mapped[float] = mapped_column(Float, default=0)  # 同龄同性别人群百分位
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)   # 各维度得分+明细
    weaknesses: Mapped[list] = mapped_column(JSON, default=list)   # 短板清单（含先天/后天标记）
    predictions: Mapped[dict] = mapped_column(JSON, default=dict)  # 当前各距离预测
    career_ceiling: Mapped[dict] = mapped_column(JSON, default=dict)  # 生涯上限

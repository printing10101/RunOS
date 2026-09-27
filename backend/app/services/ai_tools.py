"""AI 教练工具层：把引擎/数据装配能力包装成 LLM 可调用的工具（Function Calling）。

设计原则（与 README 的架构说明一致）：
- 数据类工具只读，返回引擎算出的真实数字，LLM 不得自行编造或计算配速/心率；
- 动作类工具只「提议」：校验 + 生成变更预览，不落库；真正写入必须走
  routers/ai.py 的 /api/ai/proposals/apply（用户在前端点击确认后调用），
  应用时用同一套校验函数重检，保证 LLM 无法绕过引擎闸门。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..data import (
    active_plan,
    activities_dicts,
    athlete_dict,
    body_metrics_dicts,
    build_evaluation,
    build_prediction,
    build_training_status_for,
    diet_analysis_payload,
    get_default_athlete,
    plan_adherence_dict,
    planned_workouts,
    strength_tests_dicts,
    weekly_km,
)
from ..logging_config import get_logger
from . import evaluator, planner, vdot, zones
from . import strength as strength_svc
from .vocab import is_hard_or_long

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

logger = get_logger(__name__)


# ---------------------------------------------------------------- 工具 schema（OpenAI function calling 格式）

TOOLS_SCHEMA: list[dict] = [
    {"type": "function", "function": {
        "name": "get_athlete_profile",
        "description": "获取跑者档案：年龄/身高体重/心率参数/训练年限，以及当前活动目标（项目/成绩/日期）和近期周跑量。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_training_paces",
        "description": "获取当前 Danielas 五大训练配速区间（E/M/T/I/R 的配速范围与用途）和储备心率五区间。回答任何『配速/心率该跑多少』问题必须用它。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_performance_prediction",
        "description": "获取各标准距离（800m~全马）的当前成绩预测、临界速度与数据质量说明。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_assessment",
        "description": "获取综合评估：五维画像得分（有氧/力量/意志品质/恢复/天赋）、总分等级与人群百分位、短板清单（区分先天/可训练）、跑者类型、生涯上限。数据计算较重，仅在用户问到评估/短板/天赋/上限时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_recovery_status",
        "description": "获取恢复状态：近 28 天 HRV/静息心率/睡眠与前期对照、急慢性负荷比 ACWR、恢复得分，以及最近 7 天逐日身体指标。用户提到疲劳/受伤/状态差/能不能练时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_training_status",
        "description": "获取训练状态面板：当前训练状态标签（维持/效率良好/效率不佳/巅峰/恢复中/负荷过高/中断）、训练准备度评分及构成、恢复剩余时间、7 天/28 天负荷与 ACWR、负荷重点（低强度有氧/高强度有氧/无氧占比）、耐力得分与爬坡得分。用户问『我现在是什么状态/该练还是歇/负荷是不是太大』时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_body_metrics",
        "description": "获取每日身体数据趋势：HRV/睡眠时长与分数/静息心率/体重体脂/血氧/呼吸率/压力/身体电量的近期均值与最新值。用户问体重变化、睡眠质量、HRV 趋势等身体数据时必用。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "回看天数，默认 28，范围 7-90", "default": 28},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_recent_training",
        "description": "获取近期训练概况：近 N 天（默认 14）跑步次数/总距离/总时长/平均配速、近 8 周每周跑量、以及强度分布（对照 80/20 原则）。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "回看天数，默认 14，范围 3-60", "default": 14},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_this_week_workouts",
        "description": "获取当前训练计划中本周（含今天）的逐日课表：日期/类型/标题/距离/时长/结构化步骤摘要/完成状态。用户问『今天/这周练什么』或要求调整课表前必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_plan_overview",
        "description": "获取当前训练计划整体概览：名称/目标/起止日期/可行性评估/分阶段周列表（每周目标跑量与课表标题）。",
        "parameters": {"type": "object", "properties": {
            "weeks_ahead": {"type": "integer", "description": "从本周起向后展示几周，默认 4，范围 1-30", "default": 4},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_diet_status",
        "description": "获取饮食状态：当日按课型动态计算的营养目标（热量/蛋白/碳水/脂肪）、近 14 天达成度分析、能量平衡（摄入 vs 目标/运动消耗，含近 7 天均值）、补给状态 fueling（ok/low/deficit 及降档理由）与最近记录摘要。用户问饮食/营养/体重/吃/补给，或 fueling 为 low/deficit 需要联动训练建议时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_schedule_slots",
        "description": "获取每周固定日程空档：可训练时段与忙碌时段（星期几/开始时间/时长）。判断某天有没有时间训练、能否挪课必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_strength_status",
        "description": "获取力量评估：各器械 1RM 折算、相对力量等级、肌群平衡诊断。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "propose_move_workout",
        "description": "提议把某节训练课挪到本周的另一天（只做校验与预览，不直接修改课表）。返回通过校验的变更提案或明确的失败原因。用户确认后才真正生效。",
        "parameters": {"type": "object", "properties": {
            "workout_id": {"type": "integer", "description": "要挪动的训练课 ID（来自 get_this_week_workouts）"},
            "target_weekday": {"type": "integer", "description": "目标星期几：0=周一 … 6=周日（须在同一个月计划周内）"},
        }, "required": ["workout_id", "target_weekday"]},
    }},
    {"type": "function", "function": {
        "name": "propose_quality_adjustment",
        "description": "提议调整某节质量课/长距离课的重复组数（如间歇 6 组减到 4 组、巡航间歇 3 组减到 2 组）。重复步骤的配速保持引擎计算值不变，仅按校验规则增减组数并重算距离时长。用户确认后才生效。",
        "parameters": {"type": "object", "properties": {
            "workout_id": {"type": "integer", "description": "要调整的训练课 ID"},
            "target_reps": {"type": "integer", "description": "目标重复组数，2-12"},
        }, "required": ["workout_id", "target_reps"]},
    }},
    {"type": "function", "function": {
        "name": "propose_easy_replacement",
        "description": "提议把某节质量课/长距离课整节替换为同等距离的轻松跑（只做校验与预览，不直接修改课表）。新配速由引擎按当前 VDOT 计算。适用于用户疲劳、状态差、想降低强度但保留跑量的场景。用户确认后才生效。",
        "parameters": {"type": "object", "properties": {
            "workout_id": {"type": "integer", "description": "要替换的训练课 ID"},
        }, "required": ["workout_id"]},
    }},
    {"type": "function", "function": {
        "name": "propose_add_workout",
        "description": "提议在计划周期内的某天追加一节引擎生成的轻松跑（只做校验与预览，不直接修改课表）。"
                       "距离受当天日程空档约束（上限 8km），配速按当前 VDOT 计算。"
                       "用户说『这周想加一次有氧/轻松跑』『周X有空想多跑一次』时必用。用户确认后才生效。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string",
                     "description": "加课日期 YYYY-MM-DD（把『周X/明天/后天』等相对时间解析为具体日期）"},
        }, "required": ["date"]},
    }},
    {"type": "function", "function": {
        "name": "propose_skip_workout",
        "description": "提议跳过某节计划课（应用后记为 skipped，保留记录但不算完成）。只允许 planned 状态的课。"
                       "用户说『周X去不了』『临时有事』『这节不练了』时必用；若要跳过的是强度课/长距离，"
                       "应先向用户说明影响，并给出替代方案（减组数 propose_quality_adjustment / 换轻松跑 "
                       "propose_easy_replacement）供选择。用户确认后才生效。",
        "parameters": {"type": "object", "properties": {
            "workout_id": {"type": "integer", "description": "要跳过的训练课 ID（来自 get_this_week_workouts）"},
        }, "required": ["workout_id"]},
    }},
    {"type": "function", "function": {
        "name": "propose_profile_update",
        "description": "提议更新跑者档案（个人信息）：姓名/体重/身高/静息心率/最大心率/HRV基准/出生年份/性别/系统训练年限。"
                       "用户在对话中提到这类信息时（如『我体重85kg了』『最大心率195』『我叫小王』）必用，禁止只在回答里口头记录。"
                       "若返回 needs_user_confirm=true，说明新值与档案差异异常，必须先向用户确认是真实变化还是异常数据："
                       "用户确认为真实后携带 confirm_anomaly=true 重新调用生成提案；用户否认则不生成。提案需用户在界面点击确认后才生效。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "姓名/称呼"},
            "weight_kg": {"type": "number", "description": "体重（公斤）"},
            "height_cm": {"type": "number", "description": "身高（厘米）"},
            "resting_hr": {"type": "integer", "description": "静息心率（次/分）"},
            "max_hr": {"type": "integer", "description": "最大心率（次/分）"},
            "hrv_baseline": {"type": "integer", "description": "HRV 基准值（RMSSD 毫秒）"},
            "birth_year": {"type": "integer", "description": "出生年份"},
            "sex": {"type": "string", "enum": ["male", "female"], "description": "性别"},
            "training_age_years": {"type": "number", "description": "系统训练年限（年）"},
            "confirm_anomaly": {"type": "boolean",
                                "description": "仅当用户已确认异常数值是真实数据时置 true，其余情况不要传"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "propose_goal_update",
        "description": "提议更新当前比赛目标（项目/成绩/日期）。用户提到目标变化时必用"
                       "（如「我改跑半马了」「比赛定在10月18日」「目标调成破三」），禁止只在回答里口头记下。"
                       "只传用户明确提到的字段，其余沿用当前目标；应用时会替换当前活动目标（旧目标归档）。"
                       "建议先调 precheck_goal 预检可行性再出提案。提案需用户在界面确认后生效。",
        "parameters": {"type": "object", "properties": {
            "race_type": {"type": "string", "enum": ["5k", "10k", "hm", "marathon"],
                          "description": "比赛项目（5k/10k/半马/全马）"},
            "target_time_sec": {"type": "integer",
                                "description": "目标完赛时间（秒），必须换算：全马330=3小时30分=12600、半马2小时=7200；没提成绩就不要传"},
            "target_date": {"type": "string",
                            "description": "比赛日期 YYYY-MM-DD（把『10月』『下个月』等相对时间解析为具体日期）"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_daily_checkin",
        "description": "获取今天的主观感受打卡（睡眠质量/酸痛/精力/动力/疼痛部位）、补给状态（fueling_level）与引擎给出的今日训练建议档位（正常/减量/仅轻松跑/休息），以及今天计划课的类型。用户问『今天累不累/能不能练/要不要调整』或表示状态不好、没吃够时必用；若建议档位为 reduce/easy/rest 且今天有强度课，应主动用 propose_* 工具给出调整提案。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_race_history",
        "description": "获取历史比赛实测成绩：每场的实际成绩/配速/VDOT、赛前模型预测偏差复盘，以及基于多场比赛反推的个人 Riegel 指数校准状态。用户提到比赛/跑比赛/成绩对不对/预测准不准时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_load_forecast",
        "description": "获取前瞻负荷规划：把当前训练计划代入 CTL/ATL/TSB 模型，预测未来数周体能/疲劳/形态曲线、逐周计划跑量与负荷、比赛日预计形态与风险警告。用户问『照这个计划练下去状态怎么样/比赛日什么形态/计划会不会练过头』时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_gear_status",
        "description": "获取装备（跑鞋）状态：每双累计里程、剩余寿命、是否接近/超过更换里程。用户提到跑鞋/装备/该不该换鞋时必用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "precheck_goal",
        "description": "目标可行性预检：给定项目/目标成绩/比赛日期，用引擎判断 VDOT 差距、预计所需周数是否够、日程时段能否支撑峰值长距离。用户提出新目标或改目标时必用，不要自己心算 VDOT。",
        "parameters": {"type": "object", "properties": {
            "race_type": {"type": "string", "enum": ["5k", "10k", "hm", "marathon"], "description": "比赛项目"},
            "target_time_sec": {"type": "integer", "description": "目标完赛时间（秒），如全马 3 小时 30 分 = 12600；无成绩目标可省略"},
            "target_date": {"type": "string", "description": "比赛日期 YYYY-MM-DD，可省略"},
        }, "required": ["race_type"]},
    }},
    {"type": "function", "function": {
        "name": "search_training_methods",
        "description": "检索训练方法知识库（含日本/亚洲体系：慢跑法、川内式、箱根驿传、实业团、大迫杰体系、日式课型ペース走/BUP/距离走/流し/ナンバ跑姿等，以及全球流派：挪威双阈值、极化 80/20、金字塔、汉森、Canova、Lydiard、MAF、东非）。每条含出处与证据等级。用户问『有哪些训练方法/日本怎么练/XX 流派是什么』时必用，不要凭记忆回答训练法。",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string", "enum": ["japan", "asia", "western", "global"], "description": "按来源地区过滤"},
            "level": {"type": "string", "enum": ["beginner", "intermediate", "advanced", "elite"], "description": "按适用水平过滤"},
            "race": {"type": "string", "enum": ["5k", "10k", "hm", "marathon"], "description": "按目标项目过滤"},
            "q": {"type": "string", "description": "关键词，如 川内/驿传/阈值/间歇"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "recommend_training_methods",
        "description": "按跑者画像（近 28 天周跑量/可练天数/目标/周期阶段/伤病信号，全部来自真实数据）从知识库推荐训练方法，返回得分与逐条命中理由。用户问『我适合哪种练法/给我选个方法』时必用；回答时必须带上命中理由与证据等级，不得省略 cautions。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_method_workout_detail",
        "description": "获取某个训练方法体系的完整详情（核心原则/课表模板列表/出处/优缺点/禁忌/亚洲适配依据）。先经 search_training_methods 拿到 code，再调用本工具。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "方法 code（来自 search_training_methods）"},
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "resolve_method_workout",
        "description": "把知识库课表模板解析成按用户当前 VDOT 计算真实配速的结构化步骤（可下发手表）。用户说『用川内式的间歇课给我排一节』或要求把某个流派的课落到具体配速时必用；配速由引擎计算，不得自行换算。",
        "parameters": {"type": "object", "properties": {
            "template_code": {"type": "string", "description": "课表模板 code（来自 get_method_workout_detail 的 workouts）"},
        }, "required": ["template_code"]},
    }},
    {"type": "function", "function": {
        "name": "search_training_principles",
        "description": "检索训练原理库（负荷管理/强度分布/阈值/减量/力量/补糖/睡眠恢复/步频/专项化等 12 条，含机制、可执行规则、平台已落地位置与文献出处）。用户问『为什么这么练/有没有科学依据』，或你在定制/调整训练计划需要说明决策依据时必用——给计划每个关键决策引用对应原理的 practical_rules，做到有迹可循。",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": "load/intensity/recovery/periodization/specificity/strength/nutrition/technique"},
            "q": {"type": "string", "description": "关键词，如 减量/ACWR/力量/补糖/步频"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "recommend_training_plans",
        "description": "按跑者画像从参考计划库（C25K/Higdon/FIRST/Hanson/Pfitzinger/Daniels 2Q/Galloway/业余阈值块等 10 个）匹配训练计划骨架，返回命中理由、周模板、长距离递进、关键课、减量方案与出处。用户要『帮我定一个训练计划』时必用：以此为骨架参照 + 结合 search_training_principles 的原理 + 用户真实 VDOT/日程做个性化，并明确说明参照了哪个计划、依据了哪些原理；不要凭空发明结构。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "preview_method_week",
        "description": "把某个训练方法体系按用户真实数据（周跑量/日程/VDOT）降档生成『一周体验课表』的预览（不落库）：质量课≤2次、长距离≤周跑量30%、配速按当前 VDOT 解析、附降档说明。用户说『用XX方法练一周试试/帮我按XX结构排一周』时必用；预览后应提示用户到「训练知识库→训练方法→详情」点击『按我的数据生成体验周』按钮正式落库（写入需用户主动确认，AI 不直接落库）。",
        "parameters": {"type": "object", "properties": {
            "method_code": {"type": "string", "description": "方法 code（来自 search_training_methods）"},
        }, "required": ["method_code"]},
    }},
    {"type": "function", "function": {
        "name": "review_method_week",
        "description": "复盘当前进行中的方法体验周：统计各课完成情况、近 7 天打卡的酸痛/疼痛信号、ACWR，给出『继续该方法 / 再试一轮补齐 / 回归默认引擎或换低强度方法』的结构化建议（附完成率数据与伤病信号）。用户说『体验周跑完了/下周怎么办/这套方法适不适合我』时必用；结论必须基于返回的完成率与伤病信号，不得凭感觉。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]

TOOL_LABELS = {
    "get_athlete_profile": "查询跑者档案",
    "get_training_paces": "查询训练配速表",
    "get_performance_prediction": "查询成绩预测",
    "get_assessment": "计算综合评估",
    "get_recovery_status": "查询恢复状态",
    "get_training_status": "查询训练状态面板",
    "get_body_metrics": "查询身体数据趋势",
    "get_recent_training": "查询近期训练",
    "get_this_week_workouts": "查询本周课表",
    "get_plan_overview": "查询计划概览",
    "get_diet_status": "查询饮食状态",
    "get_schedule_slots": "查询日程空档",
    "get_strength_status": "查询力量评估",
    "propose_move_workout": "校验挪课方案",
    "propose_quality_adjustment": "校验课表调整",
    "propose_easy_replacement": "校验轻松跑替换方案",
    "propose_add_workout": "校验加课方案",
    "propose_skip_workout": "校验跳课方案",
    "propose_profile_update": "校验个人信息更新",
    "propose_goal_update": "校验比赛目标更新",
    "get_daily_checkin": "查询今日主观打卡与建议",
    "get_race_history": "查询比赛成绩与校准",
    "get_load_forecast": "查询前瞻负荷规划",
    "get_gear_status": "查询装备里程状态",
    "precheck_goal": "预检目标可行性",
    "search_training_methods": "检索训练方法知识库",
    "recommend_training_methods": "按画像推荐训练方法",
    "get_method_workout_detail": "查询方法体系详情",
    "resolve_method_workout": "解析课表模板真实配速",
    "search_training_principles": "检索训练原理与依据",
    "recommend_training_plans": "匹配参考训练计划骨架",
    "preview_method_week": "预览方法体验周课表",
    "review_method_week": "复盘体验周并给下一步建议",
}


# ---------------------------------------------------------------- 内部工具实现

def _get_athlete(db: Session) -> models.Athlete:
    """服务层口径：无档案抛 ValueError（routers/deps.require_athlete 是 404 口径）。"""
    a = get_default_athlete(db)
    if not a:
        raise ValueError("尚未创建跑者档案")
    return a


def active_slots(db: Session, athlete_id: int) -> list[dict]:
    rows = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == athlete_id, models.WeeklySlot.kind == "available")).all()
    return [{"weekday": s.weekday, "start_time": s.start_time, "duration_minutes": s.duration_minutes,
             "label": s.label} for s in rows]


def _steps_summary(structured: list[dict]) -> str:
    """把结构化步骤压成一句人话，省 token 又够 LLM 转述。

    配速标签优先从数值字段（sec/km）推导，不信任历史数据里的 from_label/to_label
    （旧版 planner 存在单位错误产生的垃圾标签）。
    """
    def pace_label(tgt: dict) -> str:
        if tgt.get("type") == "pace" and tgt.get("from"):
            lo, hi = sorted((tgt["from"], tgt.get("to") or tgt["from"]))
            lo_m, lo_s = divmod(int(lo), 60)
            hi_m, hi_s = divmod(int(hi), 60)
            return f"{lo_m}:{lo_s:02d}-{hi_m}:{hi_s:02d}/km"
        return tgt.get("label") or ""

    parts = []
    for s in structured or []:
        if s["step_type"] == "active":
            qty = (f"{s['duration_value']/1000:g}km" if s.get("duration_type") == "distance"
                   else f"{s['duration_value']:g}min")
            label = pace_label(s.get("target") or {})
            parts.append(f"{s['name']} {qty} {label}".strip())
        elif s["step_type"] == "strength":
            parts.append(s["name"])
    return " → ".join(parts) if parts else "（无结构化步骤）"


def _workout_dict(wo: models.PlanWorkout, with_steps: bool = True) -> dict:
    return {
        "workout_id": wo.id, "date": wo.date.isoformat(), "weekday": wo.date.weekday(),
        "weekday_name": WEEKDAY_NAMES[wo.date.weekday()],
        "start_time": wo.start_time, "session_type": wo.session_type, "title": wo.title,
        "distance_km": wo.distance_km, "duration_min": wo.duration_min,
        "status": wo.status,
        **({"steps_summary": _steps_summary(wo.structured)} if with_steps else {}),
    }


# ---- 数据类工具 ----

def _t_profile(db: Session, athlete: models.Athlete, args: dict) -> dict:
    goal = db.scalar(select(models.Goal).where(
        models.Goal.athlete_id == athlete.id, models.Goal.status == "active").order_by(models.Goal.id))
    acts = activities_dicts(db, athlete.id, days=28)
    return {
        "profile": athlete_dict(athlete),
        "weekly_km_avg_recent": round(weekly_km(acts), 1),
        "active_goal": None if not goal else {
            "race_type": goal.race_type, "target_label": goal.target_label,
            "target_time_sec": goal.target_time_sec,
            "target_date": goal.target_date.isoformat() if goal.target_date else None,
        },
    }


def _t_paces(db: Session, athlete: models.Athlete, args: dict) -> dict:
    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        return {"ok": False, "reasons": ["暂无足够训练数据估算当前 VDOT，请先同步/录入近期的跑步记录"]}
    return {"current_vdot": pred.current_vdot,
            "daniels_paces": vdot.daniels_paces(pred.current_vdot),
            "hr_zones": zones.hr_zones(athlete.max_hr, athlete.resting_hr)}


def _t_prediction(db: Session, athlete: models.Athlete, args: dict) -> dict:
    pred = build_prediction(db, athlete.id)
    return {"current_vdot": pred.current_vdot,
            "predictions": pred.predictions, "critical_speed": pred.critical_speed,
            "data_quality": pred.data_quality}


def _t_assessment(db: Session, athlete: models.Athlete, args: dict) -> dict:
    ev = build_evaluation(db, athlete)
    return {
        "total_score": ev["total_score"], "grade": ev["grade"], "percentile": ev["percentile"],
        "dimensions": {k: {"score": d.get("score"), "evidence": d.get("evidence", [])}
                       for k, d in ev["dimensions"].items()},
        "weakness": ev["weakness"], "runner_type": ev["runner_type"],
        "career_ceiling": ev["career_ceiling"],
        "intensity_distribution": ev["intensity_distribution"],
        "weekly_km_avg": ev["weekly_km_avg"],
    }


def _t_recovery(db: Session, athlete: models.Athlete, args: dict) -> dict:
    metrics = body_metrics_dicts(db, athlete.id, days=56)
    acts = activities_dicts(db, athlete.id, days=35)
    ev = evaluator.eval_recovery(metrics, acts)
    recent = [m for m in metrics if m["date"] >= (date.today() - timedelta(days=7))]
    recent = sorted(recent, key=lambda m: m["date"], reverse=True)
    baseline = athlete.hrv_baseline
    hrv_last = next((m["hrv_rmssd"] for m in recent if m.get("hrv_rmssd") is not None), None)
    return {
        "recovery": ev,
        "hrv_baseline_setting": baseline,
        "hrv_latest_vs_baseline": (round((hrv_last - baseline) / baseline * 100) if
                                   (hrv_last and baseline) else None),
        "recent_days": [{"date": m["date"].isoformat(), "sleep_hours": m.get("sleep_hours"),
                         "hrv_rmssd": m.get("hrv_rmssd"), "resting_hr": m.get("resting_hr"),
                         "weight_kg": m.get("weight_kg")} for m in recent],
    }


def _t_recent_training(db: Session, athlete: models.Athlete, args: dict) -> dict:
    days = max(3, min(60, int(args.get("days") or 14)))
    acts = activities_dicts(db, athlete.id, days=days)
    runs = [a for a in acts if a["sport"] == "run"]
    total_km = sum(a["distance_m"] for a in runs) / 1000
    total_min = sum(a["duration_sec"] for a in runs) / 60
    pace = (total_min / total_km) if total_km > 0 else None   # min/km
    # 近 28/56 天窗口从最大窗口结果切片复用，避免一次调用查三遍库
    wide = activities_dicts(db, athlete.id, days=56)
    acts28 = wide if days >= 28 else activities_dicts(db, athlete.id, days=28)
    return {
        "days": days, "run_count": len(runs),
        "total_km": round(total_km, 1), "total_min": round(total_min),
        "avg_pace_str": vdot.pace_str(1000.0 / pace) if pace else None,   # pace(m/km)⁻¹ → m/min
        "other_sports": {s: sum(1 for a in acts if a["sport"] == s)
                         for s in {a["sport"] for a in acts} - {"run"}},
        "weekly_km_series_8w": evaluator.weekly_km_series(wide, 8),
        "intensity_distribution_28d": zones.intensity_distribution(
            acts28, max_hr=athlete.max_hr, resting_hr=athlete.resting_hr),
    }


def _t_this_week(db: Session, athlete: models.Athlete, args: dict) -> dict:
    plan = active_plan(db)
    if not plan:
        return {"has_plan": False, "hint": "当前没有进行中的训练计划"}
    today = date.today()
    week = next((w for w in plan.weeks if w.start_date <= today < w.start_date + timedelta(days=7)), None)
    if not week:
        return {"has_plan": False, "hint": f"计划 {plan.start_date.isoformat()}~{plan.race_date.isoformat()} 不覆盖本周"}
    today_wd = today.weekday()
    return {
        "has_plan": True, "plan_name": plan.name,
        "week_index": week.week_index, "phase": week.phase, "focus": week.focus,
        "week_target_km": week.target_km,
        "today": today.isoformat(),
        "workouts": [_workout_dict(wo) for wo in week.workouts],
        "rest_days": [WEEKDAY_NAMES[d] for d in range(7)
                      if not any(wo.date.weekday() == d for wo in week.workouts) and d >= 0],
        "note_past_days": "今天的日期是 " + today.isoformat() + "，早于今天的课只能作为历史参考",
        "_today_weekday": today_wd,
    }


def _t_plan_overview(db: Session, athlete: models.Athlete, args: dict) -> dict:
    plan = active_plan(db)
    if not plan:
        return {"has_plan": False}
    weeks_ahead = max(1, min(30, int(args.get("weeks_ahead") or 4)))
    today = date.today()
    cur_index = next((w.week_index for w in plan.weeks if w.start_date <= today < w.start_date + timedelta(days=7)), 1)
    weeks = [w for w in plan.weeks if cur_index <= w.week_index < cur_index + weeks_ahead]
    return {
        "has_plan": True, "plan_id": plan.id, "name": plan.name, "race_type": plan.race_type,
        "target_time_sec": plan.target_time_sec,
        "start_date": plan.start_date.isoformat(), "race_date": plan.race_date.isoformat(),
        "weekly_km_peak": plan.weekly_km_peak, "feasibility": plan.feasibility,
        "adherence_8w": plan_adherence_dict(db, athlete.id),
        "weeks": [{"week_index": w.week_index, "start_date": w.start_date.isoformat(),
                   "phase": w.phase, "focus": w.focus, "target_km": w.target_km,
                   "workouts": [_workout_dict(wo, with_steps=False) for wo in w.workouts]}
                  for w in weeks],
    }


def _t_diet(db: Session, athlete: models.Athlete, args: dict) -> dict:
    payload = diet_analysis_payload(db, athlete)
    logs = payload.get("logs", [])
    balance = payload.get("balance") or {}
    return {"targets": payload["targets"], "analysis": payload["analysis"],
            "today": payload.get("today"), "fueling": payload.get("fueling"),
            "balance_7d": balance.get("avg7"), "balance_series": balance.get("series"),
            "recent_logs": logs[-9:]}


def _t_slots(db: Session, athlete: models.Athlete, args: dict) -> dict:
    rows = db.scalars(select(models.WeeklySlot).where(
        models.WeeklySlot.athlete_id == athlete.id).order_by(models.WeeklySlot.weekday)).all()
    return {"slots": [{"weekday": s.weekday, "weekday_name": WEEKDAY_NAMES[s.weekday],
                       "start_time": s.start_time, "duration_minutes": s.duration_minutes,
                       "kind": s.kind, "label": s.label} for s in rows]}


def _t_strength(db: Session, athlete: models.Athlete, args: dict) -> dict:
    tests = strength_tests_dicts(db, athlete.id)
    return {"tests": tests,
            "analysis": strength_svc.assess_strength(tests, athlete.sex, athlete.weight_kg)}


# ---- 提议类工具（校验 + 预览，不落库） ----

def _week_of(wo: models.PlanWorkout) -> models.PlanWeek:
    return wo.week


def check_move(db: Session, athlete: models.Athlete, workout_id: int, target_weekday: int) -> dict:
    """挪课校验。返回 {ok, proposal|reasons, warnings}。应用端点复用同一函数。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or wo.athlete_id != athlete.id:
        return {"ok": False, "reasons": ["训练课不存在"]}
    if wo.status not in ("planned",):
        return {"ok": False, "reasons": [f"该课状态为 {wo.status}，只有 planned 状态的课可以挪动"]}
    if not (0 <= target_weekday <= 6):
        return {"ok": False, "reasons": ["target_weekday 必须在 0-6"]}

    week = _week_of(wo)
    new_date = week.start_date + timedelta(days=target_weekday)
    reasons, warnings = [], []

    if new_date < date.today():
        reasons.append(f"目标日期 {new_date.isoformat()} 已经过去")
    if new_date == wo.date:
        return {"ok": False, "reasons": ["这节课本来就在这一天"]}

    # 当天已有课？
    others = [x for x in week.workouts if x.date == new_date and x.id != wo.id]
    if others:
        reasons.append("挪入日已安排：" + "、".join(f"{x.title}（{x.start_time}）" for x in others))

    # 时段是否放得下
    slots = [s for s in active_slots(db, athlete.id) if s["weekday"] == target_weekday]
    if not slots:
        reasons.append(f"{WEEKDAY_NAMES[target_weekday]}没有可训练时段（日程管理中该日无 available 空档）")
    else:
        best = max(s["duration_minutes"] for s in slots)
        need = wo.duration_min * 0.85
        if best < need:
            reasons.append(f"该日最长空档 {best} 分钟，不足完成本课（约需 {round(need)} 分钟）")

    # 质量课间隔：挪入日前后 1 天是否有另一节质量课/长距离/力量
    # （vocab 统一口径：方法库计划的 interval/tempo/fartlek/hill 也算质量课）
    if is_hard_or_long(wo.session_type):
        near = [x for x in week.workouts
                if x.id != wo.id and is_hard_or_long(x.session_type)
                and abs((x.date - new_date).days) <= 1]
        if near:
            warnings.append("与 " + "、".join(f"{x.date.isoformat()} {x.title}" for x in near)
                            + " 相邻过近，连续强度日会推高伤病风险；若确需如此建议降低其中一节的强度")

    if reasons:
        return {"ok": False, "reasons": reasons, "warnings": warnings}

    return {"ok": True, "warnings": warnings, "proposal": {
        "kind": "move_workout",
        "title": f"把「{wo.title}」从 {wo.date.isoformat()}（{WEEKDAY_NAMES[wo.date.weekday()]}）挪到 "
                 f"{new_date.isoformat()}（{WEEKDAY_NAMES[target_weekday]}）",
        "workout_id": wo.id,
        "from": {"date": wo.date.isoformat(), "start_time": wo.start_time},
        "to": {"date": new_date.isoformat(), "weekday": target_weekday,
               "start_time": max(slots, key=lambda s: s["duration_minutes"])["start_time"] if slots else wo.start_time},
        "summary": _steps_summary(wo.structured),
    }}


def check_quality_adjustment(db: Session, athlete: models.Athlete, workout_id: int, target_reps: int) -> dict:
    """质量课组数调整校验：识别课表中重复的步骤循环，按目标组数重建（配速保持引擎值）。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or wo.athlete_id != athlete.id:
        return {"ok": False, "reasons": ["训练课不存在"]}
    if wo.status != "planned":
        return {"ok": False, "reasons": [f"该课状态为 {wo.status}，只有 planned 状态的课可以调整"]}
    if not is_hard_or_long(wo.session_type) or wo.session_type == "strength":
        return {"ok": False, "reasons": ["只有质量课/长距离课支持组数调整"]}
    target_reps = int(target_reps)
    if not (2 <= target_reps <= 12):
        return {"ok": False, "reasons": ["目标组数须在 2-12 之间"]}

    steps: list[dict] = [dict(s) for s in (wo.structured or [])]
    found = _detect_repeat_cycle(steps)
    if not found:
        return {"ok": False, "reasons": ["未能在这节课里识别出可重复的组（没有循环结构）"]}
    cycle, head_len, body_end, reps_now = found

    if target_reps == reps_now:
        return {"ok": False, "reasons": [f"当前就是 {reps_now} 组"]}

    new_steps = steps[:head_len] + [dict(s) for _ in range(target_reps) for s in cycle] + steps[body_end:]
    _renumber(new_steps[head_len: head_len + target_reps * len(cycle)], len(cycle), target_reps)

    # 主课（循环段）之外的距离/时长保持原值，只叠加循环段的增减
    km_cycle_now, km_cycle_new = _steps_km(steps[head_len:body_end]), _steps_km(new_steps[head_len:head_len + target_reps * len(cycle)])
    min_cycle_now, min_cycle_new = _steps_minutes(steps[head_len:body_end]), _steps_minutes(new_steps[head_len:head_len + target_reps * len(cycle)])
    km_now = max(0.0, wo.distance_km - km_cycle_now) + km_cycle_now
    km_new = max(0.0, wo.distance_km - km_cycle_now) + km_cycle_new
    dur_now = max(0.0, wo.duration_min - min_cycle_now) + min_cycle_now
    dur_new = max(0.0, wo.duration_min - min_cycle_now) + min_cycle_new
    delta_km, delta_min = round(km_new - km_now, 1), round(dur_new - dur_now)

    if wo.session_type == "long" and km_new < 8:
        return {"ok": False, "reasons": ["调整后距离不足 8km，长距离课失去意义，建议整课取消或换轻松跑"]}

    return {"ok": True,
            "warnings": (["这是减量调整：状态不好时宁可少一组，配速不掉更重要"] if target_reps < reps_now
                         else ["这是加量调整：请先确认近期负荷（ACWR）没有超标"]),
            "proposal": {
                "kind": "quality_adjustment",
                "title": f"把「{wo.title}」从 {reps_now} 组调整为 {target_reps} 组"
                         + (f"（{delta_km:+g}km / {delta_min:+g}min）" if delta_km or delta_min else ""),
                "workout_id": wo.id, "date": wo.date.isoformat(),
                "reps": {"from": reps_now, "to": target_reps},
                "distance_km": {"from": round(km_now, 1), "to": round(km_new, 1)},
                "duration_min": {"from": round(dur_now), "to": round(dur_new)},
                "new_steps_preview": _steps_summary(new_steps),
            },
            "_internal": {"new_steps": new_steps, "new_km": round(km_new, 1), "new_dur": round(dur_new)},
            }


def _detect_repeat_cycle(steps: list[dict]) -> tuple[list[dict], int, int, int] | None:
    """在步骤序列中寻找最长的重复段（周期 1-3，至少重复 2 次），返回 (cycle, 段起点, 段终点, 重复次数)。

    兼容三类课表：[热身 | 间歇×N | 冷身]、[节奏跑… | strides×N | 冷身]、[激活×N | 冷身]。
    比较用模板（组号数字化、忽略恢复时长差异，最后一组常无恢复），而非严格字典相等。
    """
    import re

    def tpl(s: dict) -> tuple:
        name = re.sub(r"\d+", "N", s.get("name", ""))
        dur = None if s["step_type"] == "rest" else s.get("duration_value")
        tgt = s.get("target") or {}
        return (s["step_type"], name, s.get("duration_type"), dur,
                tgt.get("type"), tgt.get("from"), tgt.get("to"), tgt.get("label"))

    tpls = [tpl(s) for s in steps]
    n = len(steps)
    best: tuple[int, int, int] | None = None      # (reps, period, start)
    for p in (1, 2, 3):
        for i in range(0, n - p * 2 + 1):
            reps = 1
            while i + (reps + 1) * p <= n and tpls[i + reps * p: i + (reps + 1) * p] == tpls[i: i + p]:
                reps += 1
            if reps >= 2 and any(steps[k]["step_type"] in ("active", "strength")
                                 for k in range(i, i + p)):
                if best is None or reps * p > best[0] * best[1]:
                    best = (reps, p, i)
    if not best:
        return None
    reps, p, start = best
    return steps[start:start + p], start, start + reps * p, reps


def _renumber(block: list[dict], period: int, reps: int) -> None:
    """把 x/y 形式的步骤名组号重排（如 间歇 3/6 → 间歇 3/4）。"""
    import re
    idx = 0
    for i, s in enumerate(block):
        m = re.search(r"(\d+)/(\d+)", s["name"])
        if m and i % period == 0 and s["step_type"] == "active":
            idx += 1
            s["name"] = re.sub(r"\d+/\d+", f"{idx}/{reps}", s["name"])


def _steps_km(steps: list[dict]) -> float:
    return sum(s["duration_value"] for s in steps
               if s["step_type"] == "active" and s.get("duration_type") == "distance") / 1000


def _steps_minutes(steps: list[dict], fallback_pace_sec: float | None = None) -> float:
    """估算结构化课的总时长（分钟）。

    距离步缺配速目标时用 ``fallback_pace_sec``（调用方按本人 VDOT 解析出的配速）；
    仍然没有就跳过该步并少算一点时长。不退回 330s/km 这类与用户无关的常数——
    那会让步行速度的跑者和 3 分配速的跑者拿到同一个时长估算。
    """
    total = 0.0
    for s in steps:
        if s.get("duration_type") == "time":
            total += s["duration_value"]
        elif s["step_type"] == "active" and s.get("duration_type") == "distance":
            pace_from = (s.get("target") or {}).get("from") or fallback_pace_sec
            if not pace_from:
                continue
            total += s["duration_value"] / 1000 * pace_from / 60
    return total


def check_easy_replacement(db: Session, athlete: models.Athlete, workout_id: int) -> dict:
    """把质量课/长距离课整节替换为引擎生成的轻松跑（按当前 VDOT 计算配速，距离受日程时段约束）。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or wo.athlete_id != athlete.id:
        return {"ok": False, "reasons": ["训练课不存在"]}
    if wo.status != "planned":
        return {"ok": False, "reasons": [f"该课状态为 {wo.status}，只有 planned 状态的课可以替换"]}
    if not is_hard_or_long(wo.session_type) or wo.session_type == "strength":
        return {"ok": False, "reasons": ["轻松跑/力量课无需替换，本来就是低强度"]}

    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        return {"ok": False, "reasons": ["暂无足够训练数据估算当前 VDOT，无法生成配速合法的替换课，请先录入近期跑步记录"]}
    # 距离受当天可用时段约束（与 planner 排课封顶同口径，常量来源见
    # planner.SLOT_CAP_MIN_PER_KM）。无空档时拒绝而非按全量距离放行（与 check_move 口径一致）
    slots = [s for s in active_slots(db, athlete.id) if s["weekday"] == wo.date.weekday()]
    slot_min = max((s["duration_minutes"] for s in slots), default=0)
    if not slot_min:
        return {"ok": False, "reasons": [
            f"{WEEKDAY_NAMES[wo.date.weekday()]}没有可训练时段（日程管理中该日无 available 空档）"]}
    target_km = round(min(wo.distance_km, slot_min / planner.SLOT_CAP_MIN_PER_KM), 1)
    if target_km < 3:
        return {"ok": False, "reasons": [f"当天最长空档仅 {slot_min} 分钟，放不下有意义的轻松跑"]}
    _, title, steps, km, dur = planner.build_easy_run(pred.current_vdot, target_km)
    return {"ok": True,
            "warnings": ["替换为轻松跑后，本周强度刺激减少；若处于强化期/巅峰期频繁替换会影响目标进度"],
            "proposal": {
                "kind": "easy_replacement",
                "title": f"把「{wo.title}」替换为「{title}」（配速按当前 VDOT {round(pred.current_vdot, 1)} 由引擎计算）",
                "workout_id": wo.id, "date": wo.date.isoformat(),
                "replacement": {"title": title, "distance_km": km, "duration_min": round(dur),
                                "steps_summary": _steps_summary(steps)},
            },
            "_internal": {"new_steps": steps, "new_km": km, "new_dur": round(dur), "new_title": title},
            }


def check_add_workout(db: Session, athlete: models.Athlete, target_date: str) -> dict:
    """加课校验：在计划周期内的某天追加一节引擎生成的轻松跑（距离受日程时段约束）。

    与其他提议工具同闸门：这里只校验与出预览，不落库；应用端点用同一函数以
    当前库内状态重检后写入（VDOT 变化时以应用那一刻的引擎值为准）。
    """
    try:
        d = date.fromisoformat(str(target_date)[:10])
    except ValueError:
        return {"ok": False, "reasons": [f"加课日期 {target_date!r} 无法解析，需要 YYYY-MM-DD"]}
    plan = active_plan(db)
    if not plan:
        return {"ok": False, "reasons": ["当前没有进行中的训练计划，无法加课"]}
    if d < date.today():
        return {"ok": False, "reasons": [f"加课日期 {d.isoformat()} 已经过去"]}
    week = next((w for w in plan.weeks if w.start_date <= d < w.start_date + timedelta(days=7)), None)
    if not week:
        return {"ok": False, "reasons": [f"{d.isoformat()} 不在当前计划周期"
                                         f"（{plan.start_date.isoformat()}~{plan.race_date.isoformat()}）内"]}
    same_day = [x for x in week.workouts if x.date == d]
    if same_day:
        return {"ok": False, "reasons": ["当天已有安排："
                                         + "、".join(f"{x.title}（{x.start_time}）" for x in same_day)]}

    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        return {"ok": False, "reasons": ["暂无足够训练数据估算当前 VDOT，无法生成配速合法的加课，请先录入近期跑步记录"]}
    slots = [s for s in active_slots(db, athlete.id) if s["weekday"] == d.weekday()]
    slot_min = max((s["duration_minutes"] for s in slots), default=0)
    if not slot_min:
        return {"ok": False, "reasons": [
            f"{WEEKDAY_NAMES[d.weekday()]}没有可训练时段（日程管理中该日无 available 空档）"]}
    target_km = round(min(8.0, slot_min / planner.SLOT_CAP_MIN_PER_KM), 1)   # 与排课/换轻松跑同口径
    if target_km < 3:
        return {"ok": False, "reasons": [f"当天最长空档仅 {slot_min} 分钟，放不下有意义的轻松跑"]}

    _, title, steps, km, dur = planner.build_easy_run(pred.current_vdot, target_km)
    start_time = max(slots, key=lambda s: s["duration_minutes"])["start_time"]
    warnings = []
    if plan.weekly_km_peak and week.target_km + km > plan.weekly_km_peak:
        warnings.append(f"加课后本周约 {round(week.target_km + km, 1)}km，"
                        f"超过计划峰值跑量 {plan.weekly_km_peak}km，请确认负荷可承受")
    return {"ok": True, "warnings": warnings, "proposal": {
        "kind": "add_workout",
        "title": f"在 {d.isoformat()}（{WEEKDAY_NAMES[d.weekday()]}）追加「{title}」",
        "date": d.isoformat(),
        "summary": f"{title}：约 {round(dur)} 分钟，配速按当前 VDOT {round(pred.current_vdot, 1)} 由引擎计算",
        "added": {"title": title, "start_time": start_time, "distance_km": km,
                  "duration_min": round(dur), "steps_summary": _steps_summary(steps)},
        "warnings": warnings,
    }, "_internal": {"week_id": week.id, "date": d, "title": title, "start_time": start_time,
                     "steps": steps, "km": km, "dur": round(dur)}}


def check_skip_workout(db: Session, athlete: models.Athlete, workout_id: int) -> dict:
    """跳课校验：planned → skipped（保留记录，不计入完成）。只校验与预览，不落库。"""
    wo = db.get(models.PlanWorkout, workout_id)
    if not wo or wo.athlete_id != athlete.id:
        return {"ok": False, "reasons": ["训练课不存在"]}
    if wo.status != "planned":
        return {"ok": False, "reasons": [f"该课状态为 {wo.status}，只有 planned 状态的课可以跳过"]}
    warnings = []
    if is_hard_or_long(wo.session_type) and wo.session_type != "strength":
        warnings.append("要跳过的是强度课/长距离，本周关键刺激会少一节；"
                        "若只是状态波动，可考虑减组数或换轻松跑，保留一点刺激")
    return {"ok": True, "warnings": warnings, "proposal": {
        "kind": "skip_workout",
        "title": f"跳过 {wo.date.isoformat()}（{WEEKDAY_NAMES[wo.date.weekday()]}）的「{wo.title}」",
        "workout_id": wo.id,
        "date": wo.date.isoformat(),
        "summary": _steps_summary(wo.structured) if wo.structured else wo.title,
        "warnings": warnings,
    }}


# ---------------------------------------------------------------- 档案更新提议工具（对话 → 个人信息提案）

# 档案字段规格：cast 把 LLM 传来的参数统一转成安全类型；hard 是硬范围（出界直接拒绝）；
# anomaly 是与当前档案的相对变化阈值（超过即视为可疑数据：先让 AI 向用户确认真伪，
# 确认后才允许出提案）。出生年份/性别/训练年限不设异常阈值：年龄增长、改资料属正常
# 演进，由提案确认制兜底。
PROFILE_FIELD_SPECS: dict[str, dict] = {
    "name":               {"label": "姓名", "cast": lambda v: str(v).strip(),
                           "hard": None,       "anomaly": None},   # 长度上限见实现（与档案页一致）
    "weight_kg":          {"label": "体重", "cast": float, "hard": (25, 300),  "anomaly": 0.10},
    "height_cm":          {"label": "身高", "cast": float, "hard": (80, 250),  "anomaly": 0.05},
    "resting_hr":         {"label": "静息心率", "cast": lambda v: int(round(float(v))),
                           "hard": (30, 120),  "anomaly": 0.15},
    "max_hr":             {"label": "最大心率", "cast": lambda v: int(round(float(v))),
                           "hard": (120, 240), "anomaly": 0.10},
    "hrv_baseline":       {"label": "HRV基准", "cast": lambda v: int(round(float(v))),
                           "hard": (10, 150),  "anomaly": 0.30},
    "birth_year":         {"label": "出生年份", "cast": lambda v: int(round(float(v))),
                           "hard": None,       "anomaly": None},   # 动态范围，见实现
    "sex":                {"label": "性别", "cast": str, "hard": ("male", "female"), "anomaly": None},
    "training_age_years": {"label": "系统训练年限", "cast": float, "hard": (0, 80), "anomaly": None},
}
_PROFILE_UNIT = {"weight_kg": "kg", "height_cm": "cm", "hrv_baseline": "ms",
                 "training_age_years": "年"}
_SEX_CN = {"male": "男", "female": "女"}


def _fmt_profile_val(field: str, v) -> str:
    if field == "sex":
        return _SEX_CN.get(v, str(v))
    return f"{v:g}" if isinstance(v, float) else str(v)


def check_profile_update(athlete: models.Athlete, changes: dict,
                         confirm_anomaly: bool = False) -> dict:
    """档案字段更新校验（AI 对话中的个人信息 → 更新提案）。

    与课表提案同闸门口径：这里只做校验与预览，不落库；应用端点（routers/ai.py
    /proposals/apply）用同一函数以 confirm_anomaly=True 重检后写入。范围出界直接
    拒绝；与当前档案相对变化超过阈值的字段视为可疑异常数据，未带 confirm_anomaly
    时不生成提案，返回 needs_user_confirm 让 AI 先向用户确认真伪。
    """
    changes = {k: v for k, v in (changes or {}).items()
               if k in PROFILE_FIELD_SPECS and v is not None}
    if not changes:
        return {"ok": False, "reasons": ["没有可识别的档案字段（支持：姓名/体重/身高/静息心率/"
                                         "最大心率/HRV基准/出生年份/性别/训练年限）"]}

    reasons, anomalies, change_items, applied = [], [], [], {}
    cur_year = date.today().year
    for field, spec in PROFILE_FIELD_SPECS.items():
        if field not in changes:
            continue
        try:
            val = spec["cast"](changes[field])
        except (TypeError, ValueError):
            reasons.append(f"{spec['label']}：无法把 {changes[field]!r} 解析为合法数值")
            continue
        # 各字段专属校验（不通过则拒绝该字段）
        if field == "sex":
            if val not in spec["hard"]:
                reasons.append(f"性别只能是 male/female，收到 {val!r}")
                continue
        elif field == "name":
            if not val:
                reasons.append("姓名不能为空")
                continue
            if len(val) > 20:
                reasons.append(f"姓名过长（{len(val)} 字，最多 20 字）")
                continue
        else:
            lo, hi = (cur_year - 100, cur_year - 10) if field == "birth_year" else spec["hard"]
            if not (lo <= val <= hi):
                reasons.append(f"{spec['label']} {val} 超出合理范围（{lo}-{hi}），请核对")
                continue
        applied[field] = val
        # 变更检测 + 异常判定（所有字段共用）
        cur = getattr(athlete, field, None)
        if cur is None or val == cur:
            continue
        change_items.append({"field": field, "label": spec["label"], "from": cur, "to": val})
        thr = spec["anomaly"]
        if thr and cur != 0 and abs(val - cur) / abs(cur) >= thr:
            anomalies.append({"field": field, "label": spec["label"], "change_pct":
                              round(abs(val - cur) / abs(cur) * 100)})

    if not applied:
        return {"ok": False, "reasons": reasons or ["没有产生有效的档案字段"]}
    if not change_items:
        return {"ok": False, "reasons": ["新值与当前档案一致，无需更新"]}

    # 交叉校验：更新后的最大心率必须仍高于静息心率（两者同时改时用新值对判）
    merged_max = applied.get("max_hr", athlete.max_hr)
    merged_rest = applied.get("resting_hr", athlete.resting_hr)
    if merged_max and merged_rest and merged_max <= merged_rest:
        reasons.append(f"最大心率({merged_max})必须高于静息心率({merged_rest})")
    if reasons:
        return {"ok": False, "reasons": reasons}

    def _fmt(it: dict) -> str:
        return (f"{it['label']}：{_fmt_profile_val(it['field'], it['from'])} → "
                f"{_fmt_profile_val(it['field'], it['to'])}{_PROFILE_UNIT.get(it['field'], '')}")

    summary = "；".join(_fmt(it) for it in change_items)

    if anomalies and not confirm_anomaly:
        detail = "；".join(f"{a['label']}变化 {a['change_pct']}%" for a in anomalies)
        return {"ok": False, "needs_user_confirm": True, "anomalies": anomalies,
                "reasons": [f"检测到可疑的异常数据：{detail}"],
                "hint": "新值与当前档案差异异常。先向用户确认这是真实变化还是异常数据"
                        "（如实测体重/设备误记）：用户确认为真实后再携带 confirm_anomaly=true"
                        " 重新调用本工具生成提案；用户否认则不要生成提案。"}

    warnings = (["该数值与原档案差异较大，用户已确认为真实数据（非异常记录）"]
                if anomalies else [])
    return {"ok": True, "warnings": warnings, "proposal": {
        "kind": "profile_update",
        "title": "更新个人信息：" + summary,
        "summary": summary,
        "changes": change_items,
        "warnings": warnings,
    }, "_internal": {"applied": applied}}


# ---------------------------------------------------------------- 目标更新提议工具（对话 → 比赛目标提案）

_GOAL_RACE_NAMES = {"5k": "5公里", "10k": "10公里", "hm": "半程马拉松", "marathon": "全程马拉松"}
# 各项目目标成绩的合理区间（秒）：下限≈接近精英，上限≈走跑结合完赛
_GOAL_TIME_RANGE = {"5k": (720, 3600), "10k": (1500, 7200),
                    "hm": (3000, 18000), "marathon": (7200, 36000)}
_GOAL_CHANGE_KEYS = ("race_type", "target_time_sec", "target_date", "target_label")


def check_goal_update(db: Session, athlete: models.Athlete, changes: dict) -> dict:
    """比赛目标更新校验（AI 对话 → 目标提案）。

    语义：整体替换当前活动目标（应用时旧目标归档、新建一条，与 AI 建计划流程的
    目标处理一致）；用户未提到的字段沿用当前目标值，因此缺 race_type 时必须有
    活动目标可沿用。与课表/档案提案同闸门：这里只校验与出预览，不落库；
    应用端点用同一函数重检后写入。
    """
    changes = {k: v for k, v in (changes or {}).items()
               if k in _GOAL_CHANGE_KEYS and v not in (None, "")}
    if not changes:
        return {"ok": False, "reasons": ["没有可识别的目标字段（支持：比赛项目/目标成绩/比赛日期）"]}
    goal = db.scalar(select(models.Goal).where(
        models.Goal.athlete_id == athlete.id,
        models.Goal.status == "active").order_by(models.Goal.id))
    if goal is None and "race_type" not in changes:
        return {"ok": False, "reasons": ["当前没有活动目标，请先明确比赛项目（5k/10k/半马/全马）"]}

    race_type = changes.get("race_type") or goal.race_type
    if race_type not in _GOAL_RACE_NAMES:
        return {"ok": False, "reasons": [f"比赛项目必须是 5k/10k/hm/marathon，收到 {race_type!r}"]}

    sec = goal.target_time_sec if goal else None
    if "target_time_sec" in changes:
        try:
            sec = int(round(float(changes["target_time_sec"])))
        except (TypeError, ValueError):
            return {"ok": False, "reasons": [f"目标成绩 {changes['target_time_sec']!r} 无法解析为秒数"]}
        lo, hi = _GOAL_TIME_RANGE[race_type]
        if not (lo <= sec <= hi):
            return {"ok": False, "reasons": [
                f"{_GOAL_RACE_NAMES[race_type]}目标成绩 {vdot.time_str(sec)} 超出合理范围"
                f"（{vdot.time_str(lo)}-{vdot.time_str(hi)}）；注意成绩要换算成秒（如全马330=12600秒）"]}

    d = goal.target_date if goal else None
    if "target_date" in changes:
        try:
            d = date.fromisoformat(str(changes["target_date"])[:10])
        except ValueError:
            return {"ok": False, "reasons": [f"比赛日期 {changes['target_date']!r} 无法解析，需要 YYYY-MM-DD"]}
    today = date.today()
    if d and d < today:
        return {"ok": False, "reasons": [f"比赛日期 {d.isoformat()} 早于今天，请确认日期"]}

    cur_race = goal.race_type if goal else None
    cur_sec = goal.target_time_sec if goal else None
    cur_date = goal.target_date if goal else None
    cur_label = goal.target_label if goal else ""
    label = str(changes.get("target_label") or "").strip()
    if goal is None or race_type != cur_race or sec != cur_sec:
        # 项目/成绩变了 → 标签按引擎格式重生成
        label = _GOAL_RACE_NAMES[race_type] + (f" {vdot.time_str(sec)}" if sec else "")
    elif not label:
        label = cur_label   # 无实质变更时沿用旧标签，避免把标签重生成误判成变更
    items = []
    if goal is None or race_type != cur_race:
        items.append({"field": "race_type", "label": "项目",
                      "from": "（无目标）" if goal is None else _GOAL_RACE_NAMES.get(cur_race, cur_race),
                      "to": _GOAL_RACE_NAMES[race_type]})
    if sec != cur_sec:
        items.append({"field": "target_time_sec", "label": "目标成绩",
                      "from": vdot.time_str(cur_sec) if cur_sec else "（未设）",
                      "to": vdot.time_str(sec) if sec else "（未设）"})
    if d != cur_date:
        items.append({"field": "target_date", "label": "比赛日期",
                      "from": cur_date.isoformat() if cur_date else "（未设）",
                      "to": d.isoformat() if d else "（未设）"})
    if not items and label == cur_label:
        return {"ok": False, "reasons": ["新目标与当前目标一致，无需更新"]}

    warnings = []
    if d and (d - today).days < 42:
        warnings.append(f"距比赛日仅 {(d - today).days} 天（不足 6 周），负荷需要循序渐进，别硬冲")
    plan = active_plan(db)
    if plan and (plan.race_type != race_type or (d and plan.race_date != d)):
        warnings.append("当前训练计划仍对应旧目标，目标生效后建议到「训练计划」页重新生成计划")

    summary = "；".join(f"{it['label']}：{it['from']} → {it['to']}" for it in items)
    return {"ok": True, "warnings": warnings, "proposal": {
        "kind": "goal_update",
        "title": f"更新比赛目标：{label}" + (f"（{d.isoformat()}）" if d else ""),
        "summary": summary,
        "changes": items,
        "warnings": warnings,
    }, "_internal": {"new_values": {"race_type": race_type, "target_time_sec": sec,
                                    "target_date": d, "target_label": label}}}


def precheck_goal_impl(db: Session, athlete: models.Athlete, args: dict) -> dict:
    race_type = args.get("race_type")
    if race_type not in ("5k", "10k", "hm", "marathon"):
        return {"ok": False, "reasons": ["race_type 必须是 5k/10k/hm/marathon"]}
    target_sec = args.get("target_time_sec")
    target_date = args.get("target_date")
    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        return {"ok": False, "reasons": ["暂无足够训练数据估算当前 VDOT，请先同步/录入近期跑步记录后再预检目标"]}
    # 天赋响应速度来自真实评估，不用默认值
    acts = activities_dicts(db, athlete.id)
    talent = evaluator.eval_talent(pred, acts, date.today().year - athlete.birth_year,
                                   athlete.sex, athlete.training_age_years,
                                   athlete.weight_kg, athlete.height_cm).get("score")
    if not talent:
        return {"ok": False, "reasons": ["训练数据不足以评估天赋响应速度，请先积累更多训练记录"]}
    slots = active_slots(db, athlete.id)
    if not slots:
        return {"ok": False, "reasons": ["日程管理中还没有可训练时段，请先添加"]}
    start = date.today()
    end = date.fromisoformat(target_date) if target_date else start + timedelta(weeks=16)
    if end <= start:
        return {"ok": False, "reasons": ["比赛日期早于今天"]}
    weeks = max(4, min(30, (end - start).days // 7))
    feasibility = planner.assess_feasibility(
        race_type, target_sec, pred.current_vdot, talent, weeks, slots)
    feasibility["talent_score"] = talent
    feasibility["target_date"] = end.isoformat()
    return {"ok": True, "feasibility": feasibility}


# ---------------------------------------------------------------- 训练状态/身体数据（新面板）

def _t_training_status(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from .evaluator import vdot_trend
    from .load_status import build_training_status

    acts = activities_dicts(db, athlete.id, days=200)
    metrics = body_metrics_dicts(db, athlete.id, days=60)
    ad = {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr,
          "hrv_baseline": athlete.hrv_baseline, "weight_kg": athlete.weight_kg}
    plan = active_plan(db)
    trend = vdot_trend(acts, date.today().year - athlete.birth_year, athlete.sex)
    st = build_training_status(acts, ad, metrics,
                               race_date=plan.race_date if plan else None, vdot_trend=trend)
    return {
        "status": st["status"], "readiness": st["readiness"],
        "recovery_time_h": st["recovery_time_h"], "acwr": st["acwr"],
        "load_7d": st["acute_7d"], "load_28d_weekly": st["chronic_weekly"],
        "fitness": st["fitness"], "fatigue": st["fatigue"], "form": st["form"],
        "load_focus": st["load_focus"],
        "monotony": st.get("monotony"),
        "aerobic_efficiency": st.get("aerobic_efficiency"),
        "endurance_score": st["endurance_score"], "hill_score": st["hill_score"],
        "vdot_trend_16w": trend,
    }


def _t_body_metrics(db: Session, athlete: models.Athlete, args: dict) -> dict:
    days = min(90, max(7, int(args.get("days", 28))))
    metrics = body_metrics_dicts(db, athlete.id, days=days)
    rows = [m for m in metrics if any(m.get(k) is not None for k in
            ("hrv_rmssd", "sleep_hours", "resting_hr", "weight_kg", "sleep_score",
             "spo2", "resp_rate", "stress", "body_battery", "body_fat_pct"))]
    if not rows:
        return {"days": days, "summary": {}, "recent": [], "note": "暂无身体数据，建议接入佳明同步或引导用户手动录入"}

    def avg(key, ms):
        vals = [m.get(key) for m in ms if m.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    recent7 = rows[-7:]
    summary = {k: {"recent7_avg": avg(k, recent7), "latest": next(
        (m.get(k) for m in reversed(rows) if m.get(k) is not None), None)}
        for k in ("hrv_rmssd", "sleep_hours", "sleep_score", "resting_hr",
                  "weight_kg", "body_fat_pct", "spo2", "resp_rate", "stress", "body_battery")}
    from .baseline import build_baseline
    return {"days": days, "hrv_baseline": athlete.hrv_baseline,
            "baselines": build_baseline(rows),
            "summary": summary,
            "recent": [{k: v for k, v in m.items()} for m in rows[-7:]]}


# ---------------------------------------------------------------- 新增面板工具（打卡/比赛/前瞻负荷/装备）

def _t_daily_checkin(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from .checkin_advice import build_checkin_advice

    today = date.today()
    row = db.scalar(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete.id,
        models.DailyCheckin.date == today))
    wo = db.scalars(select(models.PlanWorkout).where(
        models.PlanWorkout.athlete_id == athlete.id,
        models.PlanWorkout.date == today,
        models.PlanWorkout.status == "planned")).all()
    # 质量课/长距离优先展示（vocab 统一口径：方法库计划的 interval/tempo/fartlek/hill 也算），
    # 这些课更可能需要按打卡状态给出调整建议
    wo.sort(key=lambda w: 0 if is_hard_or_long(w.session_type) else 1)
    workout = ({"id": wo[0].id, "session_type": wo[0].session_type, "title": wo[0].title,
                "distance_km": wo[0].distance_km, "duration_min": wo[0].duration_min} if wo else None)
    checkin = None
    if row:
        checkin = {"date": row.date.isoformat(), "sleep_quality": row.sleep_quality,
                   "muscle_soreness": row.muscle_soreness, "energy_level": row.energy_level,
                   "motivation": row.motivation, "pain_area": row.pain_area, "note": row.note}
    advice = build_checkin_advice(checkin, workout, _diet_fueling_context(db, athlete),
                                  _objective_signal(db, athlete))
    if not checkin:
        advice["verdict"] += "；也可以先请用户完成今日晨间打卡（总览页）再给出更准的建议"
    return {"checkin": checkin, "advice": advice, "today_workout": workout}


def _objective_signal(db: Session, athlete: models.Athlete) -> dict:
    """客观身体信号封顶（基线引擎），与路由侧 _advice 同口径。"""
    from .baseline import objective_cap
    return objective_cap(body_metrics_dicts(db, athlete.id, days=35))


def _diet_fueling_context(db: Session, athlete: models.Athlete) -> dict | None:
    from ..data import diet_analysis_payload
    return diet_analysis_payload(db, athlete).get("fueling")


def _t_race_history(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from ..data import race_prediction_review, race_results, riegel_calibration
    from .vdot import pace_str, time_str, vdot_from_performance

    rows = race_results(db, athlete.id)
    calib = riegel_calibration(db, athlete.id)
    review = race_prediction_review(db, athlete.id)
    items = []
    for r in rows:
        try:
            v = round(vdot_from_performance(r.distance_m, r.time_sec), 1)
        except ValueError:
            v = None
        pace = round(r.time_sec / (r.distance_m / 1000))
        item = {"date": r.date.isoformat(), "race_name": r.race_name,
                "race_type": r.race_type, "distance_m": r.distance_m,
                "time_str": time_str(r.time_sec), "pace_str": pace_str(1000 / (pace / 60)),
                "vdot": v, "is_official": r.is_official}
        hit = review.get((r.date.isoformat(), r.race_type))
        if hit:
            item["prediction"] = hit   # 赛前预测快照与偏差复盘（delta_pct 正 = 比预测慢）
        items.append(item)
    return {"races": list(reversed(items)), "calibration": calib,
            "note": "录入更多不同距离的比赛（相隔≤90天）可进一步校准预测" if (calib and calib["pairs"] < 3) else None}


def _t_load_forecast(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from .load_project import project_load

    plan = active_plan(db)
    if not plan:
        return {"ok": False, "reasons": ["暂无有效训练计划，无法做前瞻负荷规划"]}
    st, _ = build_training_status_for(db, athlete)
    planned, planned_race = planned_workouts(db, plan.id)
    race_date = planned_race or plan.race_date
    fc = project_load(st["daily"], planned,
                      {"max_hr": athlete.max_hr, "resting_hr": athlete.resting_hr, "sex": athlete.sex},
                      fitness=st["fitness"], fatigue=st["fatigue"], race_date=race_date)
    return {"verdict": fc["verdict"], "warnings": fc["warnings"], "race": fc["race"],
            "weekly": fc["weekly"],
            "today_form": {"fitness": st["fitness"], "fatigue": st["fatigue"], "form": st["form"]}}


def _t_gear_status(db: Session, athlete: models.Athlete, args: dict) -> dict:
    gears = db.scalars(select(models.Gear).where(models.Gear.athlete_id == athlete.id)).all()
    if not gears:
        return {"gears": [], "note": "尚未录入装备，可在「装备」页添加跑鞋并关联跑步记录"}
    acts = db.scalars(select(models.Activity).where(
        models.Activity.gear_id.isnot(None), models.Activity.athlete_id == athlete.id)).all()
    km_by_gear: dict[int, float] = {}
    for a in acts:
        km = (a.distance_m or 0) / 1000
        if km > 0:
            km_by_gear[a.gear_id] = km_by_gear.get(a.gear_id, 0) + km
    items = []
    for g in gears:
        total = g.initial_km + km_by_gear.get(g.id, 0.0)
        wear = total / g.retire_km if g.retire_km > 0 else 0
        items.append({"name": g.name, "kind": g.kind, "status": g.status,
                      "total_km": round(total, 1), "retire_km": g.retire_km,
                      "remaining_km": round(max(0.0, g.retire_km - total), 1),
                      "wear_pct": round(min(1.5, wear) * 100),
                      "flag": ("已超期，建议更换" if wear >= 1 and g.status == "active" else
                               "接近更换里程" if wear >= 0.8 and g.status == "active" else
                               "正常" if g.status == "active" else "已退役")})
    return {"gears": items}


# ---------------------------------------------------------------- 训练方法知识库工具

def _t_search_methods(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    rows = method_library.list_methods(
        db, origin=args.get("origin"), level=args.get("level"),
        race=args.get("race"), q=args.get("q"))
    if not rows:
        return {"items": [], "note": "知识库中没有匹配的训练方法，可放宽条件或换关键词"}
    return {"total": len(rows),
            "items": [method_library.method_summary(m) for m in rows[:12]],
            "hint": "需要某个方法的完整课表/出处时，用 get_method_workout_detail 按 code 查询"}


def _t_recommend_methods(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    return method_library.recommend_payload(db, athlete, top_n=5)


def _t_method_detail(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    code = str(args.get("code") or "")
    m = method_library.get_method(db, code)
    if not m:
        return {"ok": False, "reasons": [f"知识库中不存在 code={code} 的方法，请先用 search_training_methods 查询"]}
    full = method_library.method_full(m)
    full["workouts"] = [method_library.workout_dict(w, with_structure=False) for w in full["workouts"]]
    return full


def _t_resolve_workout(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from sqlalchemy import select as _select

    from . import method_library
    code = str(args.get("template_code") or "")
    w = db.scalar(_select(models.WorkoutTemplate).where(models.WorkoutTemplate.code == code))
    if not w:
        return {"ok": False, "reasons": [f"课表模板 {code} 不存在，请先用 get_method_workout_detail 查询有效 code"]}
    pred = build_prediction(db, athlete.id)
    if not pred.current_vdot:
        return {"ok": False, "reasons": ["暂无足够训练数据估算当前 VDOT，无法计算真实配速；请先录入近期跑步记录"]}
    try:
        steps = method_library.resolve_template_steps(w.structure, pred.current_vdot)
    except ValueError as e:
        return {"ok": False, "reasons": [str(e)]}
    # 供 LLM 转述给用户的步骤摘要（不落库，仅展示）
    return {"template": method_library.workout_dict(w, with_structure=False),
            "vdot": round(pred.current_vdot, 1),
            "steps_summary": _steps_summary(steps),
            "structured": steps,
            "note": "配速已按当前 VDOT 由引擎解析；落到周计划需要走正式排课/调课流程"}


def _t_search_principles(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    rows = method_library.list_principles(db, category=args.get("category"), q=args.get("q"))
    if not rows:
        return {"items": [], "note": "原理库中没有匹配条目，可换关键词或去掉 category 过滤"}
    return {"total": len(rows),
            "items": [method_library.principle_dict(db, p) for p in rows[:12]],
            "hint": "定制计划时：把用到的原理 code 与 practical_rules 写进决策依据，做到有迹可循"}


def _t_recommend_plans(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    return method_library.recommend_plans_payload(db, athlete, top_n=4)


def _t_preview_method_week(db: Session, athlete: models.Athlete, args: dict) -> dict:
    from . import method_library
    code = str(args.get("method_code") or "")
    if not code:
        return {"ok": False, "reasons": ["缺少 method_code，请先用 search_training_methods 查询"]}
    try:
        draft = method_library.build_week_plan_from_method(db, athlete, code)
    except ValueError as e:
        return {"ok": False, "reasons": [str(e)]}
    return {"ok": True,
            "method": draft["method_name"], "vdot": draft["vdot"],
            "week_start": draft["week_start"],
            "planned_km": draft["planned_km"],
            "n_quality": draft["n_quality"],
            "workouts": [{"date": w["date"], "title": w["title"], "session_type": w["session_type"],
                          "distance_km": w["distance_km"], "duration_min": w["duration_min"],
                          "steps_summary": _steps_summary(w["structured"])}
                         for w in draft["workouts"]],
            "downgrade_notes": draft["notes"],
            "note": "这是预览；正式落库请在「训练知识库→训练方法→详情」点击『按我的数据生成体验周』，"
                    "落库后可在「训练计划」页查看并同步手表"}


def _t_review_method_week(db: Session, athlete: models.Athlete, args: dict) -> dict:
    """复盘体验周：完成率 + 打卡伤病信号 + ACWR → 结构化下一步建议。"""
    from datetime import date as _d
    from datetime import timedelta as _td

    plan = active_plan(db)
    if not plan or (plan.feasibility or {}).get("source") != "method_library":
        return {"ok": False, "reasons": ["当前没有进行中的体验周计划（计划页可生成）"]}
    method_code = (plan.feasibility or {}).get("method_code")
    week = plan.weeks[0] if plan.weeks else None
    if not week:
        return {"ok": False, "reasons": ["体验周计划缺少周数据"]}

    wos = week.workouts
    stats = {"total": len(wos),
             "completed": sum(1 for w in wos if w.status == "completed"),
             "skipped": sum(1 for w in wos if w.status == "skipped"),
             "planned": sum(1 for w in wos if w.status == "planned")}
    done = stats["completed"]
    ratio = done / stats["total"] if stats["total"] else 0.0

    since = _d.today() - _td(days=7)
    checks = db.scalars(select(models.DailyCheckin).where(
        models.DailyCheckin.athlete_id == athlete.id,
        models.DailyCheckin.date >= since)).all()
    soreness = [c.muscle_soreness for c in checks if c.muscle_soreness]
    pains = [c.pain_area for c in checks if c.pain_area]
    avg_soreness = round(sum(soreness) / len(soreness), 1) if soreness else None
    injury_signal = bool(pains) or (avg_soreness is not None and avg_soreness >= 3)

    acts = activities_dicts(db, athlete.id, days=28)
    ev = evaluator.eval_recovery(body_metrics_dicts(db, athlete.id, days=28), acts)
    acwr = (ev.get("acwr") if isinstance(ev, dict) else None) or None

    # 结构化判定（规则可解释）
    if injury_signal:
        verdict = "rest_or_switch"
        reasons = ["存在疼痛/高酸痛信号，优先恢复而不是继续换方法",
                   "建议本周只保留轻松跑（可用 slow-jog-niko-niko 体系），疼痛消失后再评估"]
    elif ratio >= 0.8 and stats["total"] > 0:
        verdict = "continue_or_progress"
        reasons = [f"完成率 {round(ratio*100)}%（{done}/{stats['total']}），执行良好",
                   "可按该方法模板的 progression 进阶一档，或再跑一轮巩固适应性"]
    elif ratio >= 0.5:
        verdict = "retry_same"
        reasons = [f"完成率 {round(ratio*100)}%（{done}/{stats['total']}），结构可行但执行有缺口",
                   "建议原样再跑一轮，优先补齐未完成的质量课/长距离"]
    else:
        verdict = "fall_back_default"
        reasons = [f"完成率 {round(ratio*100)}%（{done}/{stats['total']}），该结构当前不匹配你的生活节奏",
                   "建议回归默认排课引擎（Daniels），或换时间成本更低的方法（如 slow-jog-niko-niko）"]
    if acwr is not None and acwr > 1.5:
        reasons.append(f"注意：ACWR {acwr} 偏高，任何进阶前先回调负荷")

    return {"ok": True, "method_code": method_code, "plan_name": plan.name,
            "stats": stats, "completion_ratio": round(ratio, 2),
            "avg_soreness_7d": avg_soreness, "pain_areas": pains,
            "acwr": acwr, "verdict": verdict, "reasons": reasons,
            "note": "下一步落库仍需用户在知识库页手动确认（体验周/正式计划生成均为用户主动操作）"}


# ---------------------------------------------------------------- 注册表

ToolImpl = Callable[[Session, models.Athlete, dict], dict]

TOOL_IMPLS: dict[str, ToolImpl] = {
    "get_athlete_profile": _t_profile,
    "get_training_paces": _t_paces,
    "get_performance_prediction": _t_prediction,
    "get_assessment": _t_assessment,
    "get_recovery_status": _t_recovery,
    "get_training_status": _t_training_status,
    "get_body_metrics": _t_body_metrics,
    "get_recent_training": _t_recent_training,
    "get_this_week_workouts": _t_this_week,
    "get_plan_overview": _t_plan_overview,
    "get_diet_status": _t_diet,
    "get_schedule_slots": _t_slots,
    "get_strength_status": _t_strength,
    "propose_move_workout": lambda db, a, args: check_move(
        db, a, int(args["workout_id"]), int(args["target_weekday"])),
    "propose_quality_adjustment": lambda db, a, args: check_quality_adjustment(
        db, a, int(args["workout_id"]), int(args["target_reps"])),
    "propose_easy_replacement": lambda db, a, args: check_easy_replacement(
        db, a, int(args["workout_id"])),
    "propose_add_workout": lambda db, a, args: check_add_workout(
        db, a, str(args.get("date") or "")),
    "propose_skip_workout": lambda db, a, args: check_skip_workout(
        db, a, int(args["workout_id"])),
    "propose_profile_update": lambda db, a, args: check_profile_update(
        a, args, confirm_anomaly=bool(args.get("confirm_anomaly"))),
    "propose_goal_update": lambda db, a, args: check_goal_update(db, a, args),
    "get_daily_checkin": _t_daily_checkin,
    "get_race_history": _t_race_history,
    "get_load_forecast": _t_load_forecast,
    "get_gear_status": _t_gear_status,
    "precheck_goal": precheck_goal_impl,
    "search_training_methods": _t_search_methods,
    "recommend_training_methods": _t_recommend_methods,
    "get_method_workout_detail": _t_method_detail,
    "resolve_method_workout": _t_resolve_workout,
    "search_training_principles": _t_search_principles,
    "recommend_training_plans": _t_recommend_plans,
    "preview_method_week": _t_preview_method_week,
    "review_method_week": _t_review_method_week,
}


def execute_tool(db: Session, athlete: models.Athlete, name: str, args: dict) -> dict:
    """执行工具并返回（给 LLM 看的）结果。提议类工具剥离 _internal，附上 apply 载荷。"""
    impl = TOOL_IMPLS.get(name)
    if not impl:
        return {"ok": False, "reasons": [f"未知工具 {name}"]}
    try:
        result = impl(db, athlete, args or {})
    except ValueError as e:
        return {"ok": False, "reasons": [str(e)]}
    except Exception as e:  # 工具内部错误对 LLM 透明，便于其换路回答；但必须落日志供排障
        logger.warning("AI 工具 %s 执行失败: %s", name, e)
        return {"ok": False, "reasons": [f"工具执行失败：{e}"]}
    result.setdefault("ok", True)   # 数据类工具不显式设 ok
    result.pop("_internal", None)   # 重建的步骤不回传 LLM；应用时由服务端重算
    if result.get("ok") and result.get("proposal"):
        result["proposal"]["apply"] = _apply_payload(name, args)
    return result


def _apply_payload(tool_name: str, args: dict) -> dict:
    if tool_name == "propose_move_workout":
        return {"kind": "move_workout", "workout_id": int(args["workout_id"]),
                "target_weekday": int(args["target_weekday"])}
    if tool_name == "propose_quality_adjustment":
        return {"kind": "quality_adjustment", "workout_id": int(args["workout_id"]),
                "target_reps": int(args["target_reps"])}
    if tool_name == "propose_easy_replacement":
        return {"kind": "easy_replacement", "workout_id": int(args["workout_id"])}
    if tool_name == "propose_add_workout":
        return {"kind": "add_workout", "target_date": str(args.get("date") or "")[:10]}
    if tool_name == "propose_skip_workout":
        return {"kind": "skip_workout", "workout_id": int(args["workout_id"])}
    if tool_name == "propose_profile_update":
        clean = {k: v for k, v in args.items()
                 if k in PROFILE_FIELD_SPECS and v is not None}
        return {"kind": "profile_update", "changes": clean}
    if tool_name == "propose_goal_update":
        clean = {k: v for k, v in args.items()
                 if k in _GOAL_CHANGE_KEYS and v not in (None, "")}
        return {"kind": "goal_update", "changes": clean}
    return {}

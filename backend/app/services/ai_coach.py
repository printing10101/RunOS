"""AI 教练服务：对接本地模型（OpenAI 兼容接口）的工具调用对话循环。

职责边界：
- LLM 只负责理解意图、选择工具、组织语言；
- 所有训练数字来自 ai_tools（引擎计算），写入必须经 apply 端点人工确认。

安全边界（SSRF 防护）：ai_base_url 虽可配置，但仅允许 http(s) 且解析后落在
本机环回/私网网段的地址；本地 AI 部署只应指向用户自己的机器或局域网，
公网与链路本地（如云元数据 169.254.169.254）一律拒绝。
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Generator, Iterator
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from . import ai_tools, chat_fastpath

logger = logging.getLogger(__name__)

# 会话模式携带的最近消息数。8K 窗口时代只能带 12→24 条；窗口 32768 后动态预算
# ~25K token，40 条（单条截 4000 字符）最坏 ~48K，仍由 _trim_context 按段裁掉，
# 这里放宽只影响「带多少进站」，代价是长会话时 prefill 变慢，40 是记忆与延迟的折中。
HISTORY_LIMIT = 40
# 最大工具轮数由 settings.ai_max_tool_rounds 统一控制
MAX_TOOL_ROUNDS = settings.ai_max_tool_rounds
# 连续 N 轮「没有任何新工具被调用」（全在重复刷已有工具）即提前收尾
STALE_ROUND_LIMIT = settings.ai_stale_round_limit

# 重复调用的短路反馈：结果已在上下文里，让模型停止刷工具、直接用已有信息作答
REPEAT_CALL_HINT = ("该工具与相同参数在本轮对话中已经调用过，结果就在上文，请勿重复调用；"
                    "请直接基于已有结果回答用户。")
# 触顶/停滞后的收尾指令：禁用工具，强制模型说人话（哪怕是承认做不到）
FINALIZE_INSTRUCTION = (
    "请立即基于以上已获得的信息，用简体中文直接回答用户，不要再要求调用任何工具。"
    "如果你确实没有能完成用户请求的工具，就如实说明你做不到，并明确指出用户应该到哪个页面手动完成。"
    "不要复述或提及这段指令。"
)
# 能力闸门命中时注入本轮消息的指令：把**已经判定好的**指引交给模型组织语言。
# 与旧版「命中即断粮」不同，注入后仍保留常驻工具——用户一句话常混着在域部分
# （「帮我同步下，顺便分析下我的恢复」），查询工具让模型能答在域那部分；
# 执行类诉求由这段指令明确拒绝，模型硬拿查询工具「试探」时由重复短路/停滞早停兜住。
BOUNDARY_NOTE_INSTRUCTION = (
    "系统判定：本轮请求里有一部分超出你的能力范围。以下指引必须如实转达"
    "（可以改得更口语，但「做不到」的结论与指路的页面不得删改，也不得试图用任何工具"
    "去完成这部分被拒绝的操作）：\n{hint}\n"
    "如果用户同一句话里还问了别的问题（如查看/分析平台内已有的训练数据），"
    "照常处理那部分——需要数据就调用查询类工具。不要复述或提及这段指令本身。"
)
# 能力闸门命中时的收尾指令：把**已经判定好的**指引交给模型组织语言。
# 分工要清楚——「能不能做」由代码判定（确定性），模型只负责说人话。
FINALIZE_BOUNDARY_INSTRUCTION = (
    "本轮请求已被系统判定为超出你的能力范围，因此本轮不提供任何工具。"
    "请用简体中文直接回答用户，不要再要求调用工具，也不要编造任何执行能力。"
    "以下事实必须如实转达（可以改得更口语，但结论与指路的页面不得删改）：\n{hint}\n"
    "如果用户在同一句话里还问了别的问题，只回答你能从已有信息里确定的部分；"
    "拿不到的数据就如实说明。不要复述或提及这段指令本身。"
)
# 连收尾都失败时的最后兜底：不再甩锅给用户的「提问姿势」
FINALIZE_FAILED_MSG = (
    f"已达最大工具轮数（{MAX_TOOL_ROUNDS}）且未能生成回答。"
    "这通常说明该请求超出了 AI 教练的能力范围（如平台同步、设备连接、软件安装类操作）——"
    "同步数据请到「设置 → 平台连接」页操作，训练计划请到「训练计划」页查看或生成。"
)

# 本地模型服务鉴权 Key：优先用本项目配置 AI_API_KEY，
# 回退 LLAMA_API_KEY / LLM_API_KEY 环境变量。服务端未启用 --api-key 时可为空。
LLM_API_KEY = (
    settings.ai_api_key
    or os.environ.get("LLAMA_API_KEY", "")
    or os.environ.get("LLM_API_KEY", "")
)


def _auth_headers() -> dict:
    """需要鉴权时返回 Authorization 头，否则返回空 dict。"""
    return {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}

# 工具结果截断上限。8K 时代是 2500 字符（动态预算只有 ~1.3K token，不得不抠）；
# 窗口 32768 后动态预算 ~25K token，单条结果 8000 字符（≈3-4K token）也只占零头——
# 让模型看全引擎数据比省这点窗口值钱，砍头去尾的数据直接导致回答干瘪。
TOOL_RESULT_MAX_CHARS = 8000

# ---------------------------------------------------------------- 上下文预算（按 token，不按字符）
# 实测口径（2026-09-13，Qwen3-8B + llama-server，/v1/chat/completions 返回的 usage）：
#   · 33 个工具 schema = 11808 字符 → 5041 token（约占 8K 窗口的 62%）
#   · 系统提示词       =  2407 字符 → 1376 token
# 8K 时代静态部分合计 ≈ 6417 / 8192，动态内容只剩约 1.8K——这就是「预算必须按 token
# 卡、且不能给大」的由来：给大了并不会「宽松」，超出的部分不是在客户端被裁掉，
# 而是被 llama-server 静默丢弃 prompt 头部 —— 恰好是工具定义与系统提示词（含能力边界），
# 模型随即行为失序。这是「小模型乱扫工具」的放大器，也让提示词里的护栏形同虚设。
# 2026-09-14 起默认窗口 32768（settings.ai_context_tokens），静态占比降到 ~26%，
# 但预算逻辑不变：仍按「窗口 − 本轮实际 schema − 生成预留」推，超了照裁。
GENERATION_RESERVE_TOKENS = 1200       # 留给模型输出，占满窗口会导致生成被截断
MIN_CONTEXT_BUDGET = 1200              # 动态内容的最低预算，防止窗口配得过小时预算归零
_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def _est_tokens(text: str) -> int:
    """粗略估算 token：CJK/全角约 1 token/字，ASCII/JSON 约 3.6 字符/token。

    与实测校准（2026-09-13，读 /v1/chat/completions 的 usage）：
    · 33 个工具 schema：估 5687 / 实测 5041（高估 12.8%）
    · 系统提示词：      估 1795 / 实测 1376（高估 30.4%）
    两块都是高估，且系统提示词偏得更多——它的 ASCII 部分是 markdown 与空白，
    压缩率远高于 JSON 结构（实测约 8 字符/token）。单一系数无法同时贴合两者，
    这里刻意选保守侧：低估会让预算形同虚设，回到「被服务端静默截断」的老问题；
    高估只是少留一点历史，代价小得多。
    """
    cjk = len(_CJK_RE.findall(text))
    return int(cjk + (len(text) - cjk) / 3.6)


def _schema_tokens(schema: list[dict]) -> int:
    """一段工具 schema 的 token 估算（与下发给模型的形态一致）。"""
    return _est_tokens(json.dumps(schema, ensure_ascii=False))


TOOLS_SCHEMA_TOKENS = _schema_tokens(ai_tools.TOOLS_SCHEMA)


def _active_budget(tools_tokens: int) -> int:
    """本轮「动态内容」（对话历史 + 工具结果 + 生成）可用的 token 预算。

    必须按**本轮实际下发的 schema** 来算：工具路由裁掉一组工具后，若预算仍按全量
    schema 扣，省下来的份额就白留了，还会把本来放得下的历史误裁掉。
    """
    return max(MIN_CONTEXT_BUDGET,
               settings.ai_context_tokens - tools_tokens - GENERATION_RESERVE_TOKENS)


CONTEXT_TOKEN_BUDGET = _active_budget(TOOLS_SCHEMA_TOKENS)

SYSTEM_PROMPT = """你是「RunOS」的专属 AI 跑步教练，服务一位业余跑者。今天是 {today}（{weekday}）。

【能力边界（最高优先级，先判断「这事我能不能做」再决定要不要调工具）】
你只有两类能力：①查询平台内已有数据（工具返回引擎算出的真实数字）；②发起课表/档案/目标的变更提案（用户在界面上确认后生效）。
除此之外你没有任何执行能力——不能安装软件、不能连接或授权设备平台、不能发起数据同步、不能读写文件、不能联网、不能购买任何东西。
遇到边界外的请求，不要试图用工具绕过，直接说明做不到并指引到对应页面：
- 同步训练/身体数据、连接设备、查看同步状态 → 「设置 → 平台连接」页（连接后平台按设定间隔自动增量同步）；
- 「把高驰/手表上的课表导入本平台」→ 本平台不支持从手表反向导入课表，课表只能由本平台生成；
- 生成、查看、调整训练计划 → 「训练计划」页；把课表下发到手表 → 该页「下发手表」按钮
  （佳明/Strava 支持直连下发，高驰尚未开放训练写入，可导出 FIT 后手动导入）。
判断不了自己能不能做时，如实说明能力范围，不要靠调用一堆查询工具去「试探」。

【数字纪律】
1. 精确的个人数据（跑量/配速/心率区间/成绩/VDOT/评估分数）必须来自工具返回值，引用时注明是引擎计算值，禁止编造；
   简单算术（求和、周环比、占比、平均）可以直接算，不必为此调工具。
2. 没有对应工具的精确数字就如实说明，不要改用无关工具凑答案，也不要凭空估算成绩或配速区间。

【查询工具怎么用】
3. 用户征求建议或想全面了解自己（『给点建议』『我该怎么练』『帮我分析分析』『我现在状态如何』『今天怎么安排』）时，
   必用 get_coach_briefing 取跨域简报再作答：简报已聚合今日课表/打卡建议/训练状态/恢复/近期跑量/饮食补给/计划执行/装备告警
   与 attention 关注项，不要为这些常规数字再逐个调其他查询工具（某一维度需要更多细节时才补查）。
   给出的建议必须落到 attention 项与简报里的具体数字上，给可执行的下一步，禁止泛泛而谈。
4. 问逐次训练明细（『上周三跑了多少』『最近几次长距离什么配速』）→ get_training_history；
   要对某一次深入分析（分段配速/单课对照）→ 拿 activity_id 调 get_activity_detail。
5. 用户表示今天状态不好/很累/有疼痛时，先调 get_daily_checkin 取主观打卡与建议档位；
   建议为 reduce/easy/rest 且今天有强度课时，应主动给出对应的调整提案（减组数或换轻松跑）。
6. 饮食与训练联动：fueling_level 为 low/deficit，或用户提到没吃够/节食/体重快速下降时，先调 get_diet_status；
   近期（今天/明天）有质量课或长距离课且补给不足，主动给出调整提案，并结合当日营养目标给具体饮食补救建议。
7. 问训练方法/流派/原理（『某跑团怎么练』『为什么这么练』『哪种练法适合我』『帮我定计划』）时，必查知识库工具
   （search_training_methods / search_training_principles / recommend_training_methods / recommend_training_plans /
   get_method_workout_detail），并注明出处与证据等级；用户想『按某个方法练一周试试』时用 preview_method_week 预览，
   并指引其到知识库页点击落库按钮。

【变更提案（改课表的唯一通道）】
8. 用户要求调整课表时，必须通过提案工具表达：挪课用 propose_move_workout；增减间歇组数用 propose_quality_adjustment；
   疲劳/状态差/想降强度保跑量时用 propose_easy_replacement；想加一次课用 propose_add_workout；临时去不了用
   propose_skip_workout（跳过强度课前先说明影响并给出减组/换轻松跑的替代选项）。禁止在回答里自行给出一套
   "调整后的课表数字"代替提案——没有经过引擎校验的数字是不可信的。工具校验失败时，如实解释原因并给出替代思路。
9. 用户提到个人档案信息变化（姓名/体重/身高/静息心率/最大心率/HRV基准/出生年份/性别/训练年限，如「我体重85kg了」）时，
   必须调 propose_profile_update 生成更新提案，禁止只在回答里口头记下；若工具返回 needs_user_confirm=true
   （新值与档案差异异常），先向用户确认这是真实变化还是异常数据，得到肯定答复后携带 confirm_anomaly=true 重新调用，
   用户否认则不要生成提案。
10. 用户提出新目标或目标变化（项目/成绩/日期，如「改跑半马」「目标调成破三」）时，先调 precheck_goal 预检可行性，
    再调 propose_goal_update 生成目标提案（应用时会替换当前活动目标，旧目标归档；训练计划不会自动重生成，
    需提醒用户到计划页重新生成）。

【表达与纪律】
11. 回答用简体中文，先结论后理由，简洁、具体、有依据。
12. 如果用户的问题与跑步训练完全无关，礼貌地把话题拉回训练。
13. 严禁为了「显得做了点什么」而调用与用户问题无关的工具；同一个工具配同一组参数，在一轮对话里只能调用一次；
    工具帮不上忙时，直接回答比硬调工具正确。

【长期记忆】
14. 用户提到长期有效的信息（伤病史/运动偏好/生活节奏如『周三加班没法夜跑』/装备情况/跑步动机）时，
    主动调 remember_user_note 记住，不要等用户要求；体重/心率等档案字段仍走 propose_profile_update，
    今天的疲劳、临时安排等一次性信息不要记。
15. 回答时在相关处自然引用长期记忆里的事实（如给下肢力量课时想到旧伤），不要生硬复述记忆内容。

当前用户：{athlete_name}，{athlete_age} 岁，{sex}，当前 VDOT {vdot}。
{goal_line}
{plan_line}
{notes_line}"""

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _goal_context_line(db, athlete_id: int) -> str:
    """活动目标的一句话锚点：让每轮回答都围绕真实目标，而不是泛泛的训练建议。"""
    from sqlalchemy import select as _select
    goal = db.scalar(_select(ai_tools.models.Goal).where(
        ai_tools.models.Goal.athlete_id == athlete_id,
        ai_tools.models.Goal.status == "active").order_by(ai_tools.models.Goal.id))
    if not goal:
        return "当前未设置比赛目标（用户问目标类建议时，可引导其明确项目/成绩/日期）。"
    race = ai_tools._GOAL_RACE_NAMES.get(goal.race_type, goal.race_type)
    parts = [f"当前比赛目标：{race}"]
    if goal.target_time_sec:
        parts.append(ai_tools.vdot.time_str(goal.target_time_sec))
    if goal.target_date:
        days = (goal.target_date - _dt.date.today()).days
        parts.append(f"比赛日 {goal.target_date.isoformat()}（距今 {days} 天）")
    return "，".join(parts) + "。"


_NOTE_CATEGORY_CN = {"injury": "伤病", "life": "生活", "preference": "偏好",
                     "gear": "装备", "motive": "动机", "other": "其他"}


def _notes_context_line(db, athlete_id: int) -> str:
    """长期记忆注入（最新在前，最多 20 条）：空记忆返回空串，不占提示词。"""
    from sqlalchemy import select as _select
    rows = db.scalars(_select(ai_tools.models.CoachNote).where(
        ai_tools.models.CoachNote.athlete_id == athlete_id,
        ai_tools.models.CoachNote.active.is_(True)).order_by(
        ai_tools.models.CoachNote.updated_at.desc(),
        ai_tools.models.CoachNote.id.desc()).limit(20)).all()
    if not rows:
        return ""
    lines = "\n".join(f"- [{_NOTE_CATEGORY_CN.get(n.category, '其他')}] {n.content}"
                      for n in rows)
    return "【已记录的长期记忆】以下是你在过往对话中记住的该用户的持久事实，与当前问题相关时应主动参考：\n" + lines


def _plan_context_line(db) -> str:
    """当前计划进度的一句话锚点（plan_weeks.start_date 语义 = 这一周的周一）。"""
    from ..data import active_plan
    plan = active_plan(db)
    if not plan:
        return "当前没有进行中的训练计划。"
    today = _dt.date.today()
    week = next((w for w in plan.weeks
                 if w.start_date <= today < w.start_date + _dt.timedelta(days=7)), None)
    total = len(plan.weeks)
    if week:
        phase = f"（{week.phase}）" if week.phase else ""
        return (f"训练计划「{plan.name}」进行中：第 {week.week_index}/{total} 周{phase}，"
                f"本周目标 {week.target_km}km。")
    return (f"训练计划「{plan.name}」存在但不覆盖本周"
            f"（{plan.start_date.isoformat()}~{plan.race_date.isoformat()}）。")


# ================================================================ 工具路由（按意图裁剪工具集）
# 实测（2026-09-13）：33 个工具 schema = 5041 token，占 8K 窗口的 62%；其中带参数的
# 提案类 + 知识库类合计 3595 token（63.6%），而这两组恰好各有独立的触发语汇。
# 对 8B 模型而言，33 选 1 本身就不稳——裁剪既省窗口，也提高选择准确率。
#
# 设计原则（决定这一步安不安全，改之前先读完）：
# 1. 只裁「有独立语汇、且不会在日常查询里顺带出现」的两组：知识库、档案/目标变更；
# 2. 查询类与课表变更类工具**永远在场**（常驻集 23 个，含跨域教练简报）——它们是主力场景，
#    提示词还要求模型对疲劳/酸痛主动出提案，不能因为语汇没命中就让模型失去手；
# 3. 默认下发常驻集，命中语汇再加对应分组；追问时沿用上一轮命中的组（_recent_user_text）。
#    实测常驻集 ≈ 3000 token，比全量 5687 省 47%，动态预算从 ~1.3K 涨到 ~3.9K；
#    代价是「语汇没命中时这一轮用不了知识库/提案工具」——模型仍会作答，只是查不了库里
#    的出处。所以语汇表要尽量宽（尤其是知识库的方法名词），漏判的代价按「少一次查库」计；
# 4. 总开关三态：AI_TOOL_ROUTING 未配置时按窗口自动（≥16384 关，<16384 开），
#    显式 true/false 强制；语汇回归语料见 test_ai_tool_routing.py。
def _routing_auto_enabled() -> bool:
    """路由总开关：显式配置优先；未配置时按窗口自动判定。

    裁剪的原始动机是 8K 窗口（schema 占 62%）；32768 下全量 schema 只占 ~16%，
    继续裁的收益抵不上「语汇没命中 → 模型手里没这组工具」的失败模式，
    默认只在窗口紧张（<16384）时启用。语汇回归见 test_ai_tool_routing.py。
    """
    if settings.ai_tool_routing is not None:
        return settings.ai_tool_routing
    return settings.ai_context_tokens < 16384


TOOL_ROUTING_ENABLED = _routing_auto_enabled()

KNOWLEDGE_TOOLS: tuple[str, ...] = (
    "search_training_methods", "recommend_training_methods",
    "get_method_workout_detail", "resolve_method_workout",
    "search_training_principles", "recommend_training_plans",
    "preview_method_week", "review_method_week",
)
PROFILE_GOAL_TOOLS: tuple[str, ...] = (
    "propose_profile_update", "propose_goal_update", "precheck_goal",
)
_OPTIONAL_TOOLS: frozenset[str] = frozenset(KNOWLEDGE_TOOLS + PROFILE_GOAL_TOOLS)

# 知识库：问「某练法/流派/原理/为什么/适不适合我/帮我定计划」时才需要。
# 方法名词（汉森、亚索、80/20…）是最强信号——知识库的主题就是这些练法，
# 所以宁可把常见方法名铺进来，也不要靠「方法」两个字去猜。
KNOWLEDGE_KEYWORDS: tuple[str, ...] = (
    "方法", "流派", "训练法", "练法", "原理", "为什么", "为何", "依据", "科学",
    "研究", "出处", "证据", "文献", "期刊", "论文", "知识库", "参考计划", "计划模板",
    "体验周", "怎么练", "如何练", "该怎么练", "适合我", "哪种练", "对比", "优劣",
    "周期化", "备赛", "运动员", "跑团", "教练都",
    # 常见训练法/体系名（知识库收录的主题词）
    "汉森", "亚索", "法特莱克", "丹尼尔斯", "杰克·丹尼尔斯", "80/20", "极化",
    "maf", "lsd", "乳酸阈", "最大摄氧", "金字塔跑", "节奏跑怎么", "间歇怎么",
)
# 档案变更：铁律 10 列举的可变字段
PROFILE_KEYWORDS: tuple[str, ...] = (
    "体重", "身高", "姓名", "名字", "静息心率", "最大心率", "hrv", "出生",
    "年龄", "性别", "训练年限", "档案", "个人信息",
)
# 目标变更：铁律 11。刻意不收裸「目标」——它在日常配速/课表语境里太常见，
# 收了会让大半数请求都带上这组工具，等于没裁。
GOAL_KEYWORDS: tuple[str, ...] = (
    "比赛", "赛事", "半马", "全马", "马拉松", "破三", "破四", "破4", "sub3",
    "10k", "5k", "成绩", "报名", "中签", "改目标", "换目标", "新目标", "目标变",
)
_ROUTING_HINT_LEN = 2      # 判定语汇时看最近几条用户消息（含上一轮，兜住「那试试这个」这类追问）


def _latest_user_text(history: list[dict]) -> str:
    """最近一条有内容的用户消息（会话模式下即本轮问题）。"""
    for m in reversed(history or []):
        if m.get("role") == "user" and str(m.get("content") or "").strip():
            return str(m["content"])
    return ""


def _recent_user_text(history: list[dict], n: int = _ROUTING_HINT_LEN) -> str:
    """最近 n 条用户消息拼在一起，供路由判定——只取本轮会漏掉追问里的上下文。"""
    texts = [str(m.get("content") or "") for m in (history or [])
             if m.get("role") == "user" and str(m.get("content") or "").strip()]
    return "\n".join(texts[-n:]).lower()


def route_tool_schema(history: list[dict]) -> tuple[list[dict], str]:
    """按意图裁剪工具集，返回 (schema, 人类可读的路由说明)。

    默认下发**常驻集**（全部查询类 + 全部课表提案类，共 22 个），命中语汇再加对应分组。
    三个分组各自独立判定：只提到比赛目标时不会顺带把「档案更新」这类最贵的工具塞进来。
    """
    if not TOOL_ROUTING_ENABLED:
        return ai_tools.TOOLS_SCHEMA, "路由已关闭，下发全量"
    text = _recent_user_text(history)
    if not text:
        return ai_tools.TOOLS_SCHEMA, "无用户消息，下发全量"
    hit_knowledge = any(k in text for k in KNOWLEDGE_KEYWORDS)
    hit_profile = any(k in text for k in PROFILE_KEYWORDS)
    hit_goal = any(k in text for k in GOAL_KEYWORDS)
    keep: set[str] = set()
    if hit_knowledge:
        keep |= set(KNOWLEDGE_TOOLS)
    if hit_profile:
        keep.add("propose_profile_update")
    if hit_goal:
        keep |= {"propose_goal_update", "precheck_goal"}
    schema = [t for t in ai_tools.TOOLS_SCHEMA
              if t["function"]["name"] not in _OPTIONAL_TOOLS
              or t["function"]["name"] in keep]
    if not keep:
        return schema, f"默认（常驻集）→ {len(schema)}/{len(ai_tools.TOOLS_SCHEMA)} 个工具"
    groups = "+".join(g for g, v in (("知识库", hit_knowledge),
                                     ("档案", hit_profile), ("目标", hit_goal)) if v)
    return schema, f"命中「{groups}」→ {len(schema)}/{len(ai_tools.TOOLS_SCHEMA)} 个工具"


# ================================================================ 能力闸门（确定性，先于工具循环）
# 提示词里的能力边界靠模型「自觉」，而 8B 并不总能自觉。这里用代码做确定性判断：
# 命中即把已判定的指引作为 system 注记注入本轮消息（BOUNDARY_NOTE_INSTRUCTION），
# 常驻工具照常下发——混合请求（执行类 + 在域查询）里在域部分不至于被断粮陪葬；
# 收尾请求仍禁用工具，模型始终不产出内容时直接把指引原文交给用户。
#
# 判定必须带「祈使信号」（请/帮我/怎么… 或 动词+一下）才算命中，否则会把
# 「同步过来的数据」这类**在域描述**误判成同步请求。在域语料的零误伤回归见
# test_ai_scope_gate.py —— 那份语料是这套正则的护栏，改正则必须同时跑它。
_ADDR = r"(?:请|帮我|帮忙|麻烦|去|给我|能否|可以|能不能)"
_SOFT = r"(?:怎么|如何|怎样)"
_IMPER = r"(?:一下|下|吧|呗|赶紧|立刻|马上)"
_FILLER = r"(?:你|我|把|将|它|他们|这个|那个|一下)"
_PLATFORM_VERBS = r"同步|导入|导出|连接|绑定|授权|登录|登陆|配对|下发|上传"
_SOFTWARE_VERBS = r"安装|卸载|下载"
_BUY_VERBS = r"买|购买|下单|充值|续费|付款|支付"
_MSG_VERBS = r"发|发送|推送|通知|转发"
_MSG_TARGETS = r"微信|邮件|邮箱|短信|手机|qq|钉钉|telegram|飞书"
_FILE_TARGETS = r"\.env|env\s*文件|配置文件|配置项|数据库|源码|代码|脚本"

PLATFORM_GUIDANCE = (
    "我没有执行能力，没法发起数据同步、连接设备平台，也没法把课表下发到手表。\n"
    "· 同步训练/身体数据、连接高驰或佳明/Strava：请到「设置 → 平台连接」页操作，"
    "连接后平台会按设定间隔自动增量同步；\n"
    "· 从手表反向导入课表：本平台不支持，课表只能由本平台生成；\n"
    "· 把课表下发到手表：请到「训练计划」页用「下发手表」按钮"
    "（目前佳明/Strava 支持直连下发，高驰尚未开放训练写入，可导出 FIT 后手动导入）。"
)
SOFTWARE_GUIDANCE = (
    "我没有安装、下载或卸载软件的能力，也不能读写本机文件——这类操作需要你自己在本机完成。"
)
BUY_GUIDANCE = (
    "我没有购物、支付或下单能力，也不能访问任何外部网站。装备方面我只能从训练角度给建议"
    "（比如跑鞋类型怎么跟跑量、体重搭配），具体下单请你自己到电商平台完成。"
)
MESSAGING_GUIDANCE = (
    "我没有对外发送消息的能力——不能发微信/邮件/短信，也不能推送通知。"
    "要看课表可以在「训练计划」页查看，需要留存可以用该页的导出功能自行保存。"
)
FILE_GUIDANCE = (
    "我不能改配置、改代码，也不能读写数据库或本机文件，这些都要你自己在本机操作。"
    "如果你其实是想调整训练相关的东西（目标、周跑量、某节课的强度），直接告诉我，我走提案流程帮你改。"
)

OUT_OF_SCOPE_RULES: tuple[tuple[str, re.Pattern, str], ...] = (
    ("platform", re.compile(
        rf"(?:{_ADDR}){_FILLER}{{0,2}}(?:{_PLATFORM_VERBS})"          # 请你去同步 / 帮我连接
        rf"|(?:{_PLATFORM_VERBS}){_IMPER}"                            # 同步一下 / 导入下
        rf"|(?:{_SOFT})[^。！？\n]{{0,8}}?(?:{_PLATFORM_VERBS})"       # 怎么把课表导入
        rf"|(?:{_PLATFORM_VERBS})(?:状态|情况|成功|失败|好了|完了)"     # 数据同步成功了吗
        rf"|(?:{_ADDR}|{_SOFT})[^。！？\n]{{0,8}}?(?:同步|下发|推送)(?:到|进)"
        rf"(?:手表|高驰|佳明|garmin|coros|strava|设备|平台)"
    ), PLATFORM_GUIDANCE),
    ("software", re.compile(
        rf"(?:{_ADDR}){_FILLER}{{0,2}}(?:{_SOFTWARE_VERBS})"
        rf"|(?:{_SOFTWARE_VERBS}){_IMPER}"
        rf"|(?:{_SOFT})(?:{_SOFTWARE_VERBS})"
    ), SOFTWARE_GUIDANCE),
    ("purchase", re.compile(
        rf"(?:{_ADDR}){_FILLER}{{0,1}}(?:{_BUY_VERBS})"
        rf"|(?:{_SOFT})(?:{_BUY_VERBS})"
    ), BUY_GUIDANCE),
    # 只有点到「微信/邮件/短信…」这类外部通道才算能力外：
    # 「把课表发我看看」是在域的表达，不能被当成发消息请求（回归语料里有这条）。
    ("messaging", re.compile(
        rf"(?:{_MSG_VERBS})[^。！？\n]{{0,6}}?(?:{_MSG_TARGETS})"
    ), MESSAGING_GUIDANCE),
    ("local_files", re.compile(
        rf"(?:改|修改|编辑|删除|打开|读取|写入)[^。！？\n]{{0,8}}?(?:{_FILE_TARGETS})"
        rf"|(?:{_FILE_TARGETS})[^。！？\n]{{0,8}}?(?:改|修改|编辑|删除|打开|读取|写入)"
    ), FILE_GUIDANCE),
)


def detect_out_of_scope(text: str) -> str | None:
    """判定是否为「能力外」请求：命中返回指引文案，在域返回 None。纯确定性，不打模型。"""
    t = (text or "").lower()
    if not t:
        return None
    for name, pattern, guidance in OUT_OF_SCOPE_RULES:
        if pattern.search(t):
            logger.info("AI 能力闸门命中「%s」：%s", name, t[:40])
            return guidance
    return None


def _is_local_only(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """仅放行回环与常规私网地址。

    Python 的语义坑：169.254.0.0/16 与 fe80::/10（链路本地，云元数据
    169.254.169.254 所在段）以及 0.0.0.0/8 都会被 is_private 判为「私有」，
    必须显式排除；::ffff:x.x.x.x 形式的 IPv4 映射地址要先还原再判断，
    否则可用 IPv6 写法绕过。
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    return bool(ip.is_private)


def validate_base_url(url: str) -> str | None:
    """校验 AI 服务地址：仅允许 http(s) 且解析后属于本机/私网。

    返回 None 表示通过，否则返回拒绝原因。
    """
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return "AI 服务地址必须是合法的 http(s) URL"
        infos = socket.getaddrinfo(p.hostname, None)
        if not infos:
            return f"AI 服务地址无法解析：{p.hostname}"
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not _is_local_only(ip):
                return (f"AI 服务地址 {p.hostname} 不在本机/私网范围内，已拒绝。"
                        "本功能仅支持本地部署的模型服务")
    except socket.gaierror:
        return f"AI 服务地址无法解析：{urlparse(url).hostname}"
    except ValueError:
        return "AI 服务地址格式不正确"
    return None


def probe() -> dict:
    """探测本地模型服务是否可用。"""
    base = settings.ai_base_url.rstrip("/")
    bad = validate_base_url(base)
    if bad:
        return {"reachable": False, "model": settings.ai_model, "base_url": base,
                "models": [], "model_ready": False, "error": bad}
    try:
        r = httpx.get(f"{base}/models", headers=_auth_headers(), timeout=2.5)
        models = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
        return {"reachable": r.status_code == 200, "model": settings.ai_model,
                "base_url": base, "models": models,
                "model_ready": settings.ai_model in models}
    except Exception:
        return {"reachable": False, "model": settings.ai_model,
                "base_url": base, "models": [], "model_ready": False}


_probe_cache: dict = {}
_probe_lock = threading.Lock()
_PROBE_TTL_SEC = 5.0


def probe_cached(max_age_sec: float = _PROBE_TTL_SEC) -> dict:
    """probe 结果的短 TTL 缓存。

    点评 / 营养估算 / 参数提取这类后台路径每次生成都先同步 GET /models
    （最长 2.5s）；一次打卡触发多条生成时是纯重复探测。模型可达性秒级
    缓存足够。状态端点与 lm_manager 的就绪等待仍用实时 probe()。
    """
    now = time.monotonic()
    with _probe_lock:
        at = _probe_cache.get("at")
        if at is not None and now - at < max_age_sec:
            return _probe_cache["value"]
    value = probe()
    with _probe_lock:
        _probe_cache["at"] = now
        _probe_cache["value"] = value
    return value


def _effective_model(models: list[str]) -> str:
    """配置的模型不可用时，若服务上恰好只有一个模型则直接使用之
    （兼容 LM Studio / llama-server 各自的模型 id 命名差异）。"""
    if settings.ai_model in models:
        return settings.ai_model
    if len(models) == 1 and models[0]:
        return models[0]
    return settings.ai_model


def review_model_id(models: list[str]) -> str:
    """解读端点家族的模型选择：AI_MODEL_REVIEW 配置且服务上确实挂着才用，否则回落主模型。

    注意单槽推理代理的换模型代价（整模重载），默认留空与主模型同轨，见 config.ai_model_review。
    """
    want = (settings.ai_model_review or "").strip()
    if want and want in models:
        return want
    return _effective_model(models)


def review_gen_kwargs(default_max_tokens: int) -> dict:
    """解读家族 chat/completions 的公共生成参数：思考开关与配套 token 上限。

    思考段会先烧掉大量 token 才开始正文，开启思考时必须同步放大上限，
    否则思考没结束就被截断，_strip_think 之后只剩半句甚至空串。
    """
    if settings.ai_review_think:
        return {"think": True, "max_tokens": max(default_max_tokens, 2400)}
    return {"think": False, "max_tokens": default_max_tokens}


def review_timeout() -> float:
    """解读家族的请求超时（秒）：思考模式下放宽——实测 thinking 版 plan-review
    要 95s，默认的 min(ai_timeout, 90) 上限会让 LLM 路径必然超时而退规则版。
    """
    if settings.ai_review_think:
        return max(settings.ai_timeout, 240.0)
    return min(settings.ai_timeout, 90.0)


class ThinkFilter:
    """流式输出中过滤 <think>…</think> 思考段（部分模型即使请求关闭思考也会输出）。"""

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out = []
        while self._buf:
            if self._in_think:
                end = self._buf.find("</think>")
                if end == -1:
                    keep = min(len(self._buf), 8)   # 保留尾部，防止 </think> 被截断误判
                    self._buf = self._buf[-keep:]
                    break
                self._buf = self._buf[end + 8:]
                self._in_think = False
            else:
                start = self._buf.find("<think>")
                if start == -1:
                    keep = min(len(self._buf), 7)
                    if len(self._buf) > keep:
                        out.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + 7:]
                self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        return "" if self._in_think else rest


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).replace("<think>", "").replace("</think>", "")


def system_prompt(db: Session) -> str:
    from .ai_tools import _get_athlete
    athlete = _get_athlete(db)
    from ..data import build_prediction
    vdot = build_prediction(db, athlete.id).current_vdot
    return SYSTEM_PROMPT.format(
        today=_dt.date.today().isoformat(),
        weekday=WEEKDAY_CN[_dt.date.today().weekday()],
        athlete_name=athlete.name, athlete_age=_dt.date.today().year - athlete.birth_year,
        sex="男" if athlete.sex == "male" else "女",
        vdot=vdot if vdot else "未知",
        goal_line=_goal_context_line(db, athlete.id),
        plan_line=_plan_context_line(db),
        notes_line=_notes_context_line(db, athlete.id),
    )


def chat_stream(db: Session, history: list[dict], summary: str | None = None,
                on_summary=None) -> Iterator[dict]:
    """执行一轮对话，产出事件流：{type: delta|tool|proposals|error|done}。

    summary/on_summary：会话模式的滚动摘要（上下文压缩）。summary 为上一轮存好的
    摘要，注入消息头部；历史超预算时被裁掉的旧段会压缩成新摘要并经 on_summary
    回调持久化。无状态模式两者都传 None，退化为纯裁剪。

    契约：进程内任何终止路径（正常结束/错误/异常）都以 done 事件收尾。
    唯一例外是客户端断连：Starlette 关闭生成器时在 yield 点抛入 GeneratorExit，
    此时再 yield（哪怕是 done）会触发「generator ignored GeneratorExit」的
    RuntimeError 并打进 ASGI 层，必须静默退出。
    """
    try:
        yield from _chat_stream_impl(db, history, summary, on_summary)
    except GeneratorExit:
        raise
    else:
        yield {"type": "done"}


def _msgs_tokens(msgs: list[dict]) -> int:
    """估算整段 messages 的 token 数（含 tool_calls 的 arguments）。"""
    total = 0
    for m in msgs:
        total += _est_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += _est_tokens(str((tc.get("function") or {}).get("arguments") or ""))
    return total


def _segment_starts(msgs: list[dict]) -> list[int]:
    """把消息切成「可整体丢弃」的段，返回各段起始下标（不含 system）。

    每个 user / assistant 消息各起一段，其后的 tool 结果归属于「发起它的 assistant」
    那一段。因此整段删除永远不会把 assistant(tool_calls) 与它的 tool 拆开——
    这正是旧实现 `del msgs[1]` 会犯的错。
    """
    return [i for i, m in enumerate(msgs[1:], start=1)
            if m.get("role") in ("user", "assistant")]


_ctx_overflow_warned = False


def _warn_ctx_overflow(total: int, budget: int, tools_tokens: int) -> None:
    """超预算且已无历史可裁时的告警：进程内首次用 warning，之后降为 info。

    这不是可以「忍一忍」的噪声——它说明窗口配得比静态开销还小，
    提示词里的能力边界随时可能被服务端截断冲掉，必须去调 ai_context_tokens。
    但工具循环每轮都会撞到它，全按 warning 打会淹没日志，故只响亮一次。
    """
    global _ctx_overflow_warned
    log = logger.info if _ctx_overflow_warned else logger.warning
    log("AI 上下文超预算且已无可裁历史：%d > %d token（本轮静态部分占 %d）。"
        "请调大 settings.ai_context_tokens / llama-server --ctx-size。",
        total, budget, tools_tokens)
    _ctx_overflow_warned = True


def _trim_context(msgs: list[dict], budget: int,
                  dropped_out: list[dict] | None = None) -> int:
    """把 msgs 压回 budget（token）内，返回丢弃的段数（0 = 未裁剪）。

    dropped_out 非空时，被裁掉的消息原样收集进去（供滚动摘要使用）。

    两条不变量：
    1. 每个 role=tool 的消息必须紧跟在携带同 id tool_calls 的 assistant 之后
       （旧实现逐条从头丢，会制造「tool 消息没有前置 tool_calls」的非法序列，
       llama-server 直接 400）；
    2. 用户当前问题（最后一条 user）与最后一段（持有最新工具结果）一律保留。

    策略：从最旧的段开始丢，跳过最后一条 user 消息。丢掉的只是可重建的历史残渣，
    而它原本也会被 llama-server 的 context shift 连工具定义、系统提示词一起冲掉。

    budget 由调用方按**本轮实际下发的工具 schema** 算（见 _active_budget），
    所以这里必须收参数，不能用模块级常量——否则工具路由省下的窗口会被浪费。
    """
    dropped = 0
    while _msgs_tokens(msgs) > budget:
        starts = _segment_starts(msgs)
        droppable = starts[:-1]                    # 最后一段（最新工具结果）绝不丢
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        target = next((i for i in droppable if i != last_user), None)
        if target is None:
            # 已无从裁起。此时不再和稀泥（截断工具结果只会让模型拿到残缺数据），
            # 而是把根因暴露成一条可观测告警：窗口配小了，去调 ai_context_tokens。
            _warn_ctx_overflow(_msgs_tokens(msgs), budget, TOOLS_SCHEMA_TOKENS)
            break
        end = next((s for s in starts if s > target), len(msgs))
        if dropped_out is not None:
            dropped_out.extend(msgs[target:end])
        del msgs[target:end]
        dropped += 1
    return dropped


def _stream_once(client, base: str, model_id: str, msgs: list[dict],
                 tools: list[dict] | None = None) -> Generator[dict, None, dict]:
    """发起一次流式对话：边产出 delta 事件，最后 return 本轮结果。

    返回 {"content": 文本, "tool_calls": [...], "error": 错误文案|None}。
    tools=None 表示本轮禁用工具（收尾作答、能力闸门走的就是这条）。
    正常轮与收尾轮的流式解析完全一致，差异只在 tools 字段，
    抽到一处以免两套解析各自演化出 bug。
    """
    assistant_content = ""
    tool_calls: list[dict] = []
    think = ThinkFilter()
    body: dict = {
        "model": model_id,
        "messages": msgs,
        "stream": True,
        "think": False,           # 关闭 Qwen3 思考模式；不识别该字段的后端会忽略
        # 小模型工具选择的稳定性优先于文风发散：温度高了 8B 会漏调/错调工具
        "temperature": 0.4,
    }
    if tools:
        body["tools"] = tools
    with client.stream("POST", f"{base}/chat/completions",
                       headers=_auth_headers(), json=body) as resp:
        if resp.status_code != 200:
            detail = resp.read().decode("utf-8", "ignore")[:300]
            return {"content": "", "tool_calls": [],
                    "error": f"模型服务返回 {resp.status_code}：{detail}"}
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                assistant_content += delta["content"]
                text = think.feed(delta["content"])
                if text:
                    yield {"type": "delta", "text": text}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                while len(tool_calls) <= idx:
                    tool_calls.append({"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
                slot = tool_calls[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    tail = think.flush()
    if tail:
        yield {"type": "delta", "text": tail}
    return {"content": _strip_think(assistant_content).strip(),
            "tool_calls": tool_calls, "error": None}


# ---------------------------------------------------------------- 上下文压缩（滚动摘要）
# 长会话的旧历史此前只能被 _trim_context 整段丢弃（「截断即遗忘」）。这里改成
# 「摘要回收」：将被裁掉的旧段先压缩成要点，存进会话（ai_conversations.summary），
# 下一轮从消息头部注入。摘要是优化不是硬依赖——压缩失败退化为纯裁剪。

SUMMARY_PROMPT = """你在为一位跑者的 AI 教练压缩过往对话，产出供后续对话使用的记忆摘要。
只保留对教练有持续价值的信息：用户的事实（伤病史/目标/偏好/生活节奏/装备/成绩）、已达成的决定或共识、未解决的待办；丢弃寒暄、重复与一次性内容。
输出不超过 200 字的中文要点，可用分号分隔，不要评论、不要编号。
若下方「更早的摘要」非空，把其中仍然有效的要点合并进新摘要。
更早的摘要：{old_summary}"""

SUMMARY_HEADER = "【此前对话的要点摘要（更早的细节已省略）】\n"


def summarize_dialogue(dialog: str, old_summary: str = "") -> str | None:
    """调用本地模型把对话片段压缩成滚动摘要。失败返回 None。"""
    base = settings.ai_base_url.rstrip("/")
    if validate_base_url(base):
        return None
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": _effective_model(probe_cached()["models"]), "stream": False,
                  "think": False, "temperature": 0.2,
                  "messages": [{"role": "system",
                                "content": SUMMARY_PROMPT.format(old_summary=old_summary or "（无）")},
                               {"role": "user", "content": dialog[:6000]}]},
            headers=_auth_headers(),
            timeout=httpx.Timeout(settings.ai_timeout, connect=5.0),
        )
        r.raise_for_status()
        content = _strip_think(r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.info("对话摘要生成失败，退化为纯裁剪：%s", e)
        return None
    return content[:400] or None


def _compress_and_trim(msgs: list[dict], budget: int, summary: str | None,
                       on_summary, client, base: str, model_id: str) -> int:
    """进站时的摘要回收：历史超预算，先把将被裁掉的旧段压缩成滚动摘要，再裁剪。

    只在进站时执行一次；工具轮内的裁剪针对的是本轮工具结果，不参与摘要。
    on_summary 回调负责持久化（会话模式传存库闭包，无状态模式传 None 跳过）。
    """
    dropped: list[dict] = []
    pre = _trim_context(msgs, budget, dropped_out=dropped)
    if not pre:
        return 0
    logger.info("AI 历史超预算，回收 %d 段（当前 %d token）", pre, _msgs_tokens(msgs))
    dialog = "\n".join(f"{m['role']}: {str(m.get('content') or '')[:300]}"
                       for m in dropped if m.get("role") in ("user", "assistant"))
    new_summary = summarize_dialogue(dialog[-6000:], old_summary=summary or "")
    if not new_summary:
        return pre
    note = {"role": "system", "content": SUMMARY_HEADER + new_summary}
    if len(msgs) > 1 and msgs[1].get("role") == "system" \
            and str(msgs[1].get("content", "")).startswith(SUMMARY_HEADER):
        msgs[1] = note          # 已有旧摘要：原地替换，不叠加
    else:
        msgs.insert(1, note)
    if on_summary:
        try:
            on_summary(new_summary)
        except Exception as e:
            logger.warning("滚动摘要落库失败：%s", e)
    _trim_context(msgs, budget)   # 注入摘要后再压一次，保证总量仍在预算内
    return pre


def _finalize_answer(client, base: str, model_id: str, msgs: list[dict],
                     hint: str | None = None) -> Generator[dict, None, str]:
    """禁用工具再请求一次，强制模型用已有信息作答。

    hint 为空 = 触顶/停滞后的常规收尾；hint 非空 = 能力闸门命中，把已判定的指引
    交给模型组织语言。返回生成的自然语言回答（空字符串表示没能产出内容）。
    设计意图：宁可给出一句「我做不到 + 你该去哪儿做」，也不能像旧版那样只吐一句报错。
    """
    instruction = (FINALIZE_BOUNDARY_INSTRUCTION.format(hint=hint) if hint
                   else FINALIZE_INSTRUCTION)
    res = yield from _stream_once(
        client, base, model_id,
        msgs + [{"role": "user", "content": instruction}],
        tools=None)
    return res["content"]


def _chat_stream_impl(db: Session, history: list[dict], summary: str | None = None,
                      on_summary=None) -> Iterator[dict]:
    from .ai_tools import _get_athlete
    try:
        athlete = _get_athlete(db)
    except ValueError as e:
        yield {"type": "error", "message": str(e)}
        return

    # 第〇层·直通：确定性数据查询（今天/本周课表、配速表）不进 LLM，
    # 毫秒级返回引擎数据。判定保守（严格短句白名单 + 疑问诉求排除），
    # 不命中才继续走 ①能力闸门 → ②工具路由 → ③④循环护栏的完整链路。
    fast = chat_fastpath.answer(db, _latest_user_text(history))
    if fast:
        yield {"type": "delta", "text": fast}
        return

    status = probe()
    if not status["reachable"]:
        yield {"type": "error", "message":
               (status.get("error") or f"本地模型服务不可用（{settings.ai_base_url}）。")
               + " 请确认本地模型服务（llama.cpp llama-server）已随平台启动或已手动启动。"}
        return

    msgs: list[dict] = [{"role": "system", "content": system_prompt(db)}]
    for m in history[-HISTORY_LIMIT:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": _strip_think(str(m["content"]))[:4000]})

    proposals: list[dict] = []
    base = settings.ai_base_url.rstrip("/")
    model_id = _effective_model(status["models"])
    # 能力闸门优先于工具路由：先判「这事我能不能做」，能做才谈得上给哪几个工具。
    # 两者都是确定性的，不打模型、不额外花 token。
    latest = _latest_user_text(history)
    scope_hint = detect_out_of_scope(latest)
    tool_schema, routing_note = route_tool_schema(history)
    tool_tokens = _schema_tokens(tool_schema)
    budget = _active_budget(tool_tokens)     # 预算按本轮实际 schema 算，路由省下的窗口要还给动态内容
    logger.info("AI 工具集：%s（%d token / 全量 %d）；能力闸门：%s",
                routing_note, tool_tokens, TOOLS_SCHEMA_TOKENS,
                "命中" if scope_hint else "未命中")
    try:
        with httpx.Client(timeout=httpx.Timeout(settings.ai_timeout, connect=5.0)) as client:
            # 首轮请求前必须把历史压回预算（满编最坏 ~96K token 远超窗口 32K，不裁的话
            # 第一次 _stream_once 就会把工具 schema 与系统提示词从 prompt 头部挤掉）。
            # 裁剪前先做「摘要回收」：被裁掉的旧段压缩成滚动摘要注入头部并持久化。
            _compress_and_trim(msgs, budget, summary, on_summary, client, base, model_id)
            seen_calls: set[str] = set()   # 已执行过的「工具+参数」指纹，用于拦截重复调用
            stale_rounds = 0               # 连续「零个新工具被调用」的轮数
            rounds_used = 0
            if scope_hint:
                # 能力外/混合请求：确定性指引注入本轮消息，常驻工具照常下发（见常量注释）。
                msgs.append({"role": "system",
                             "content": BOUNDARY_NOTE_INSTRUCTION.format(hint=scope_hint)})
            for _round in range(MAX_TOOL_ROUNDS):
                rounds_used = _round + 1
                res = yield from _stream_once(client, base, model_id, msgs, tools=tool_schema)
                if res["error"]:
                    yield {"type": "error", "message": res["error"]}
                    return
                assistant_content, tool_calls = res["content"], res["tool_calls"]

                if not tool_calls:
                    # 模型给出最终回答，正常结束；空答不急着报错，先让收尾通道再试一次
                    if proposals:
                        yield {"type": "proposals", "items": proposals}
                    if assistant_content:
                        return
                    logger.info("AI 第 %d 轮未产出工具调用与内容，进入收尾作答", rounds_used)
                    break

                # 执行工具轮
                msgs.append({"role": "assistant",
                             "content": assistant_content,
                             "tool_calls": [
                                 {"id": t["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": t["function"]["name"],
                                               "arguments": t["function"]["arguments"]}}
                                 for i, t in enumerate(tool_calls)]})
                fresh = False
                for i, t in enumerate(tool_calls):
                    name = t["function"]["name"]
                    label = ai_tools.TOOL_LABELS.get(name, name)
                    raw_args = t["function"]["arguments"] or "{}"
                    fingerprint = f"{name}:{raw_args}"
                    if fingerprint in seen_calls:
                        # 重复调用：不执行、不重复喂结果，只告诉模型「这个已经查过了」
                        logger.info("AI 重复调用工具已短路：%s", fingerprint[:120])
                        yield {"type": "tool", "name": name,
                               "label": f"{label}（跳过重复）", "ok": False}
                        content = json.dumps({"ok": False, "reasons": [REPEAT_CALL_HINT]},
                                             ensure_ascii=False)
                    else:
                        seen_calls.add(fingerprint)
                        fresh = True
                        try:
                            args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            args = {}
                        result = ai_tools.execute_tool(db, athlete, name, args)
                        if name.startswith("propose_") and result.get("proposal"):
                            proposals.append(result["proposal"])
                            result = {**result, "hint": "提案已生成并展示给用户，等用户在界面上确认后生效；请基于提案内容向用户解释"}
                        yield {"type": "tool", "name": name, "label": label,
                               "ok": bool(result.get("ok"))}
                        content = json.dumps(result, ensure_ascii=False, default=str)
                        if len(content) > TOOL_RESULT_MAX_CHARS:
                            # 工具结果只留头部（字段顺序即重要性顺序）+ 尾部，避免上下文溢出
                            keep = TOOL_RESULT_MAX_CHARS // 2
                            content = (content[:keep] + "\n…[结果过长已截断]…" + content[-keep:])
                    msgs.append({"role": "tool",
                                 "tool_call_id": t["id"] or f"call_{i}",
                                 "content": content})
                dropped = _trim_context(msgs, budget)
                if dropped:
                    # 可观测性：裁剪此前完全静默（且被服务端静默截断掩盖），
                    # 出问题时只能靠用户截图复盘。
                    logger.info("AI 上下文超预算，已裁掉 %d 段历史（当前 %d token）",
                                dropped, _msgs_tokens(msgs))

                stale_rounds = 0 if fresh else stale_rounds + 1
                if stale_rounds >= STALE_ROUND_LIMIT:
                    logger.info("AI 工具循环连续 %d 轮无新调用，提前收尾", stale_rounds)
                    break

            # 走到这里 = 轮数用尽、停滞早停或空答。
            # 禁用工具做一次收尾作答：保证用户至少拿到一句人话（含能力边界说明 + 去哪手动做），
            # 而不是像旧版那样只吐一句「请换个问法」把锅甩回给用户。
            logger.info("AI 工具循环结束（已用 %d/%d 轮），进入收尾作答", rounds_used, MAX_TOOL_ROUNDS)
            if proposals:
                yield {"type": "proposals", "items": proposals}
            # 能力闸门命中的轮次，收尾指令带上指引（若模型在循环里已答了在域部分，
            # 走不到这里；走到这里说明模型始终没产出内容，指引就是最后的话）。
            answer = yield from _finalize_answer(client, base, model_id, msgs,
                                                 hint=scope_hint or None)
            if not answer:
                if scope_hint:
                    # 模型连收尾都没能产出：直接把指引原文交给用户——
                    # 它本身就是完整可执行的回答，不该再降级成 error。
                    yield {"type": "delta", "text": scope_hint}
                    return
                yield {"type": "error", "message": FINALIZE_FAILED_MSG}
    except httpx.ConnectError:
        yield {"type": "error", "message": "无法连接本地模型服务，请确认服务已启动（随平台启动会自动拉起）"}
    except (httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        yield {"type": "error", "message": f"模型响应中断：{e}。建议换更小的模型（如 qwen3-4b）"}
    except Exception as e:  # 兜底：流式响应在发出 error 之前崩溃会让前端永转圈，转成可见错误
        logger.warning("ai chat_stream 异常: %s", e, exc_info=True)
        yield {"type": "error", "message": f"对话处理出错：{e}"}


# ---------------------------------------------------------------- 自由文本 → 计划参数（第三步）

EXTRACT_PROMPT = """你是参数提取器。从用户的自然语言目标中提取结构化字段，只输出一个 JSON 对象，不要解释。
今天是 {today}。
字段：
- race_type: "800m"|"1k"|"1500m"|"3k"|"5k"|"10k"|"hm"|"marathon"（800米=800m、1000米/1公里=1k、1500米=1500m、3公里=3k；hm=半程马拉松；马拉松/全马=marathon）
- target_time_sec: 整数秒（如"破三"=10800、"330"=全马3小时30分=12600、"50分钟"10k=3000）；无法确定则 null
- target_date: "YYYY-MM-DD"（把"10月""下个月"等相对时间解析为具体日期，年份取未来最近的一个）；无法确定则 null
- target_label: 简短中文标签（如"全马破三""杭马 330"）
- weekly_km_peak: 数字或 null（用户明说周跑量目标时填）
用户输入：{text}"""


def extract_goal_params(text: str) -> dict:
    """调用本地模型把自由文本解析成 generate_plan 参数。解析失败抛 ValueError。"""
    today = _dt.date.today().isoformat()
    base = settings.ai_base_url.rstrip("/")
    bad = validate_base_url(base)
    if bad:
        raise ValueError(bad)
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": _effective_model(probe_cached()["models"]), "stream": False, "think": False,
                  "temperature": 0.1,   # 参数提取/数值估算要稳定，不要发散
                  "messages": [{"role": "system",
                                 "content": EXTRACT_PROMPT.format(today=today, text=text[:500])},
                                {"role": "user", "content": text[:500]}]},
            headers=_auth_headers(),
            timeout=httpx.Timeout(settings.ai_timeout, connect=5.0),
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise ValueError(f"本地模型不可用或解析失败：{e}") from e
    content = _strip_think(content).strip()
    if content.startswith("```"):
        content = content.split("```")[1].removeprefix("json").strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型没有返回可解析的 JSON，请换个说法描述目标")
    params = json.loads(content[start:end + 1])
    # 模型偶发返回数组/字符串：非 dict 直接按解析失败处理，而不是 AttributeError → 500
    if not isinstance(params, dict):
        raise ValueError("模型返回的参数格式异常，请换个说法描述目标")
    if params.get("race_type") not in ai_tools.vdot.GOAL_RACE_TYPES:
        raise ValueError("没能识别出比赛项目（800米～全马均可，如 800m/1k/5k/半马/全马），请明确一下")
    if params.get("target_time_sec") is not None:
        try:
            params["target_time_sec"] = int(params["target_time_sec"])
        except (TypeError, ValueError):
            # from None：底层是 int() 的报错文本，对用户没有信息量，
            # 真正要说的是下面这句格式提示
            raise ValueError("目标成绩格式无法识别，请直接写时间（如 3 小时 30 分）") from None
        lo, hi = ai_tools._GOAL_TIME_RANGE[params["race_type"]]
        if not (lo <= params["target_time_sec"] <= hi):
            raise ValueError("目标成绩超出该项目的合理范围，请检查")
    if params.get("target_date"):
        try:
            _dt.date.fromisoformat(params["target_date"])
        except ValueError:
            params["target_date"] = None
    params["target_label"] = str(params.get("target_label") or "")[:60]
    return params


# ---------------------------------------------------------------- 食物描述 → 营养素估算（第四步）

ESTIMATE_PROMPT = """你是营养估算器。根据用户描述的一餐食物，估算热量与三大营养素，只输出一个 JSON 对象，不要解释。
字段：kcal（整数，千卡）、protein_g、carb_g、fat_g（数字，克）、note（一句中文说明，点出主要食材与估算假设，20 字以内）。
按常见家庭做法与中等份量估算；描述含多样食物时逐项累加；无法识别的食物按最相近的常见食物估算。
用户描述：{text}"""


def estimate_meal_nutrition(text: str) -> dict:
    """调用本地模型按食物描述估算营养素。失败抛 ValueError（中文原因，调用方转 HTTP 错误）。"""
    base = settings.ai_base_url.rstrip("/")
    bad = validate_base_url(base)
    if bad:
        raise ValueError(bad)
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            json={"model": _effective_model(probe_cached()["models"]), "stream": False, "think": False,
                  "temperature": 0.1,   # 参数提取/数值估算要稳定，不要发散
                  "messages": [{"role": "system", "content": ESTIMATE_PROMPT.format(text=text[:500])},
                               {"role": "user", "content": text[:500]}]},
            headers=_auth_headers(),
            timeout=httpx.Timeout(settings.ai_timeout, connect=5.0),
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise ValueError(f"本地模型不可用或估算失败：{e}") from e
    content = _strip_think(content).strip()
    if content.startswith("```"):
        content = content.split("```")[1].removeprefix("json").strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型没有返回可解析的结果，请手动填写营养数字")
    raw = json.loads(content[start:end + 1])

    def _num(key: str, cap: float) -> float:
        try:
            v = float(raw.get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        return round(max(0.0, min(cap, v)), 1)

    kcal = _num("kcal", 5000)
    if kcal <= 0:
        raise ValueError("没能估算出有效热量，请手动填写")
    return {"kcal": int(kcal), "protein_g": _num("protein_g", 500),
            "carb_g": _num("carb_g", 1000), "fat_g": _num("fat_g", 500),
            "note": str(raw.get("note") or "")[:80]}

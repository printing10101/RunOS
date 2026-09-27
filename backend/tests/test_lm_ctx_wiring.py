"""llama-server 启动窗口与 AI 上下文预算必须同源（防止再次静默漂移）。

背景（2026-09-13 实测）：lm_manager 里把 --ctx-size 写死成 8192，
而 ai_coach 的预算另算一套、还是按字符给到 22000（≈ 1.3 万 token，远大于窗口）。
两边脱节的后果不是「宽松」，而是超出部分被 llama-server 静默丢弃 prompt 头部
—— 恰好是工具定义与系统提示词（能力边界护栏所在），于是护栏形同虚设。

本文件锁住这条链：启动窗口 = settings.ai_context_tokens = 预算推导的输入。
"""
from app.config import settings
from app.services import ai_coach, lm_manager


def test_ctx_size_comes_from_settings():
    """--ctx-size 必须取自配置，不得再出现写死的窗口大小。"""
    cmd = lm_manager._server_cmd("llama-server.exe", "model.gguf")
    assert "--ctx-size" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == str(settings.ai_context_tokens)
    # 缺 --jinja 时请求体里的 tools 会被内置 chat handler 忽略，模型不会产生 tool_calls
    assert "--jinja" in cmd


def test_context_budget_is_derived_not_hardcoded():
    """预算必须由「窗口 − 工具 schema − 生成预留」推出，而不是拍脑袋常数。

    实测锚点（Qwen3-8B，读 /v1/chat/completions 的 usage）：
      33 个工具 schema → 5041 token；系统提示词 → 1376 token；窗口 8192。
    也就是说静态开销已占约 78%，这正是「必须调大 ai_context_tokens」的量化依据。
    """
    expected = (settings.ai_context_tokens
                - ai_coach.TOOLS_SCHEMA_TOKENS
                - ai_coach.GENERATION_RESERVE_TOKENS)
    assert ai_coach.CONTEXT_TOKEN_BUDGET == max(1200, expected)
    assert ai_coach.TOOLS_SCHEMA_TOKENS > 3000, "33 个工具 schema 不可能只值几百 token"
    assert ai_coach.GENERATION_RESERVE_TOKENS > 0, "必须给模型输出留余量"


def test_default_window_leaves_room_for_the_system_prompt():
    """窗口再小也得装下系统提示词，否则护栏从第一轮起就注定被截断。

    实测系统提示词 ≈ 1376 token。若预算连它都装不下，说明 ai_context_tokens
    与工具规模已经不匹配——此时应当调大窗口，而不是靠裁剪历史硬扛
    （_trim_context 会在这种情况下打 warning，见 test_ai_tool_loop.py）。
    """
    assert ai_coach.CONTEXT_TOKEN_BUDGET >= 1200, "预算下限本身就不该低于 1200"
    assert ai_coach.TOOLS_SCHEMA_TOKENS + ai_coach.GENERATION_RESERVE_TOKENS \
        < settings.ai_context_tokens, "静态开销已吃光窗口，请调大 ai_context_tokens"

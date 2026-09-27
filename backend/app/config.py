from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str = f"sqlite:///{BASE_DIR / 'sport_platform.db'}"
    cors_origins: str = "http://localhost:5173,http://localhost:8000"

    # 日志级别（DEBUG / INFO / WARNING / ERROR）。排查第三方同步问题时
    # 调到 DEBUG 可看到被跳过的单项明细，见 app/logging_config.py。
    log_level: str = "INFO"

    # CSRF 防护额外放行的来源（逗号分隔，如 https://192.168.1.9:8000）。
    # 默认只允许本机来源（localhost / 127.0.0.1 / ::1），见 app/security.py；
    # 局域网访问需同时把服务绑定改回 0.0.0.0。
    csrf_extra_origins: str = ""

    # 高驰（COROS）官方 MCP 通道：个人账号可用，无需申请企业资质
    # 官方文档：https://github.com/coroslab/COROS-MCP
    # 认证：OAuth 2.0 授权码 + PKCE(S256) + 动态客户端注册(RFC 7591)，公共客户端无密钥
    # 端点按账号区域分流（官方 FAQ 4）：cn / eu / us；auto = 域名跳转自动选最佳节点
    coros_region: str = "cn"
    coros_redirect_uri: str = "http://localhost:8000/api/connections/coros/callback"

    # Garmin
    garmin_email: str = ""
    garmin_password: str = ""
    garmin_client_id: str = ""
    garmin_client_secret: str = ""

    # Strava 官方 API（个人开发者即可申请：strava.com/settings/api）
    # 高驰手表可在高驰 App 内开启「同步到 Strava」，由本适配器拉取真实逐点数据
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:8000/api/connections/strava/callback"

    # 本地 AI 教练（llama.cpp llama-server 的 OpenAI 兼容端点；仅允许本机/私网地址，
    # 见 ai_coach.validate_base_url。llama-server 随平台自动拉起，监听端口取自本 URL。
    # 默认指向 llama-server 的默认端口 1234——AI 完全可选，端点不可达时教练自动
    # 退化为规则点评，平台其余功能不受影响）
    ai_base_url: str = "http://localhost:1234/v1"
    ai_model: str = "qwen3-8b"
    # 本地 llama.cpp llama-server 的 API Key（Bearer）。
    # 留空则不带 Authorization（仅当服务端未启用 --api-key 时可用）。
    # 也回退读取 LLAMA_API_KEY / LLM_API_KEY 环境变量。
    ai_api_key: str = ""
    ai_timeout: float = 120.0
    # 配 0 会让每轮对话都撞"已达最大轮数"，最低 1
    ai_max_tool_rounds: int = max(1, 6)
    # 连续 N 轮工具调用全部与之前重复（没有任何新工具被调用）时提前收尾，
    # 不再白烧剩余轮数；小模型在无对应工具时会反复扫零参数工具，见 services/ai_coach.py
    ai_stale_round_limit: int = max(1, 2)
    # llama-server 的单次请求上下文窗口（token）。lm_manager 用它拼 --ctx-size，
    # 是「模型窗口」的唯一事实来源，不要在别处再写死数字。
    # 8K 时代的实测口径（Qwen3-8B）：33 个工具 schema ≈ 5041 token、系统提示词 ≈ 1376 token，
    # 静态部分已占 8K 窗口的约 78%——2026-09-14 起默认窗口提到 32768（静态占比降到 ~26%）。
    # 不能回退到 8192：兜底自拉起的 llama-server 会直接 exceed_context_size_error
    # （全量 schema 请求实测 prompt ≈ 11016 token），AI 教练在兜底模式下必挂。
    ai_context_tokens: int = 32768
    # 现有模型 GGUF 的绝对路径，llama-server 加载用
    ai_model_path: str = ""
    # 可选推测解码草稿模型 GGUF（如 Qwen3-0.6B）：小模型起草、大模型验证，生成提速
    ai_draft_model_path: str = ""
    # llama.cpp 解压目录（含 bin/llama-server.exe）；留空则取项目根目录下的 llama/
    ai_llama_dir: str = ""
    # AI 工具路由（按语汇裁剪下发的工具 schema）总开关，AI_TOOL_ROUTING=false 整体关掉
    ai_tool_routing: bool = True
    # 解读端点家族（评估/预测/计划/周报/饮食）的专用模型名；留空与主模型同轨
    ai_model_review: str = ""
    # 解读家族是否开思考段：开了会先烧大量 token，需同步放大上限与超时
    ai_review_think: bool = False

    # 自动同步：服务运行期间每隔 N 分钟增量拉取已连接平台（0 = 关闭）。
    # 运行时设置存数据库 app_settings，本项只是初始默认值；
    # 高驰 MCP 无 webhook 推送，定时拉取是准实时更新的手段，
    # 间隔下限见 services/syncer.py。
    auto_sync_minutes: int = 30

    model_config = {"env_file": BASE_DIR / ".env", "extra": "ignore"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

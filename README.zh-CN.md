# RunOS

[![tests](https://github.com/printing10101/RunOS/actions/workflows/tests.yml/badge.svg)](https://github.com/printing10101/RunOS/actions/workflows/tests.yml)
[![privacy-guard](https://github.com/printing10101/RunOS/actions/workflows/privacy-guard.yml/badge.svg)](https://github.com/printing10101/RunOS/actions/workflows/privacy-guard.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

本地优先的跑步训练数据平台：对接高驰 / 佳明 / Strava 同步训练与身体数据，基于日程与比赛目标生成周期化训练计划，课表可下发回手表；内置完全跑在本机的 AI 教练——不上云、不订阅，数据不离开你的电脑。

**English documentation: [README.md](README.md)。**

## 界面

| 今日 | 训练计划 |
| --- | --- |
| ![今日](docs/screenshots/dashboard.png) | ![训练计划](docs/screenshots/plan.png) |
| **训练状态** | **AI 教练** |
| ![训练状态](docs/screenshots/training-status.png) | ![AI 教练](docs/screenshots/ai-coach.png) |

## 功能

- **平台连接**：高驰（官方 MCP 通道，免申请凭据）/ 佳明 / Strava 适配器；手动同步可选范围（近 90 天 / 半年 / 近一年，按 60 天分段滚动拉取）；自动同步默认每 30 分钟增量拉取，可在「平台连接」页开关或调整间隔
- **训练记录**：距离 / 配速 / 心率 / 步频 / 爬升 / 卡路里 / 功率 / 训练效果；详情页有每公里分段、配速与 GAP 曲线、心率曲线、海拔剖面、GPS 轨迹、心率区间分布和跑步动态（步幅 / 垂直振幅 / 触地 / 左右平衡）
- **装备管理**：跑鞋累计里程、剩余寿命与更换提醒
- **比赛记录**：实测成绩录入，多场不同距离的成绩自动反推个人 Riegel 指数用于校准预测；比赛日前逐日留档预测快照，赛后自动复盘预测与实际的偏差
- **每日打卡**：晨间问卷（睡眠 / 酸痛 / 精力 / 动力 / 疼痛部位）映射为建议档位（正常 / 减量 / 仅轻松跑 / 休息），叠加补给状态（能量亏缺只降不升）与客观身体信号（HRV 相对滚动基线明显偏低、睡眠不足时同样只降不升）；AI 教练据此发起课表调整提案
- **训练状态**：状态标签、训练准备度、恢复时间、7 / 28 天负荷趋势与 ACWR、负荷重点、单调性 / 应变（Foster 体系）、有氧效率 EF、有氧解耦与疲劳抗性；前瞻负荷规划把当前计划代入 CTL / ATL / TSB 推演未来 12 周，给出比赛日形态与过劳警告
- **身体数据**：每日 HRV（含基线对照）、睡眠、静息心率、体重体脂、血氧、呼吸率、压力、身体电量；个性化基线引擎自动维护 28 天滚动中位数与波动带，异常 / 漂移自动标注，无需手填
- **饮食记录**：按天记录四餐，描述可交本地大模型估算营养素；营养目标随当天课型调整（质量课多碳水、休息日控碳）；近 14 天习惯分析与能量平衡
- **训练计划**：目标驱动（如"全马破三"）+ 日程空档 + 饮食习惯，生成基础 / 强化 / 巅峰 / 减量的周期化课表，逐日带结构化步骤与饮食提示；可导出 .ics 到 Apple / Google / Outlook 日历
- **手表下发**：结构化训练（热身 / 间歇 / 配速心率区间）下发到佳明 / Strava，App 同步后手表可直接开始；也可导出通用 FIT 文件
- **分析**：力量评估（1RM / 肌群平衡）、五维综合评估与人群百分位、跑者类型、短板诊断（区分可训练项与先天特质）、成绩预测（Riegel + VDOT + 临界速度三模型加权 + 生涯上限）

## 快速开始（Windows）

前置：[Python](https://www.python.org/downloads/) 3.10+（实测 3.14）与 [Node.js](https://nodejs.org/) 18+。

```bat
git clone https://github.com/printing10101/RunOS.git
cd RunOS
start.bat
```

`start.bat` 首次运行会自动安装后端依赖并构建前端，之后启动服务并打开浏览器（http://127.0.0.1:8000）。关闭后端窗口即停止服务。也有桌面壳（`desktop/desktop.py`，pywebview 原生窗口），关窗优雅停服。

**想先看看界面？** 灌入 27 周合成演示数据：

```bat
cd backend
python seed_demo.py    # 库里有数据时拒绝执行；--force 重灌
```

### Linux / macOS

一键入口仅限 Windows，后端是标准 FastAPI 应用：

```bash
cd backend && pip install -r requirements.txt && python run.py   # 127.0.0.1:8000
cd web && npm install && npm run build                           # 构建产物由后端托管
```

## 接入真实平台

| 平台 | 方式 | 说明 |
| --- | --- | --- |
| **高驰** | 官方 [Build on COROS MCP](https://github.com/coroslab/COROS-MCP) 通道——「平台连接」页点"官方接口接入"完成授权，**无需申请任何凭据**（首次授权自动完成 OAuth 客户端注册）。`backend/.env` 配 `COROS_REGION`。 | 活动 + 每日身体数据；课表下发暂未开放（可导 FIT 手动导入） |
| **佳明** | 社区库 `garminconnect` + Garmin Connect 个人账号（`GARMIN_EMAIL` / `GARMIN_PASSWORD`）。 | 非官方接口，仅个人自用，官方改版可能导致端点变动 |
| **Strava** | [strava.com/settings/api](https://www.strava.com/settings/api) 免费申请个人应用，回调地址填 `http://localhost:8000/api/connections/strava/callback`。 | 逐点 GPS / 心率数据最好的来源（高驰列表接口不含逐点数据） |

详见 [docs/integration-guide.md](docs/integration-guide.md)。不接平台、纯手动录入也完全可用。

## AI 教练（可选，本地大模型）

不配置 AI 一切功能照常，教练自动退化为规则点评。开启需要 [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server` 和一个 GGUF 模型（Qwen3-8B / Qwen3-14B 的 `Q4_K_M` 量化是性价比起点）：

1. 准备好 `llama-server.exe` 和 `.gguf` 模型文件；
2. `backend/.env` 配 `AI_MODEL_PATH`（GGUF 绝对路径）与 `AI_LLAMA_DIR`（llama.cpp 目录），全部选项见 `backend/.env.example`；
3. 重启平台。AI 教练页显示「已连接」即成功——模型服务随平台启动、关窗自动停掉释放显存。

架构原则：**LLM 出意图，引擎出数字，用户做确认。** 模型从不编造配速、负荷或预测——它通过 26 个查询工具调用 VDOT / 区间 / 规划引擎取真实计算结果；任何课表改动都先生成「提案卡片」，写入前服务端还会重新校验一次。

## 开发

```bash
cd backend && python -m pytest -q        # 约 470 个测试
python -m ruff check .                   # 在仓库根执行
cd web && npm test && npm run lint       # vitest + eslint
```

CI 在 Windows 上跑后端套件（平台的交付目标系统），外加前端测试、静态检查与[隐私扫描](tools/privacy_guard.py)。参与贡献请顺手装上本地隐私钩子——它们拦截 GPS 轨迹、设备 ID、凭据等个人数据入库：

```bash
git config core.hooksPath githooks
```

## 文档

- [docs/algorithms.md](docs/algorithms.md) — 评分、预测与负荷算法说明
- [docs/integration-guide.md](docs/integration-guide.md) — 平台接入指南

## 免责声明

成绩预测、天赋评估、生涯上限均为基于公开运动科学模型（Daniels、Riegel、临界速度）与统计启发式的工程估计，随数据积累滚动修正，**不构成专业医疗/训练建议**。训练状态类指标为对标佳明、高驰方法论的工程近似；佳明接入使用社区非官方接口。

## 许可

[MIT](LICENSE)

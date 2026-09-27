# 平台接入指南（高驰 / 佳明 / Strava）

所有凭据都只存本机 `backend/.env`（SQLite 明文，单用户本地应用）。配好后重启后端，
在「平台连接」页完成授权即可。自动同步默认每 30 分钟增量拉取（`AUTO_SYNC_MINUTES`，
可在「平台连接」页随时开关或调整；0 = 关闭）。

## 高驰 COROS（官方 MCP 通道，无需申请凭据）

- 走 Build on COROS MCP 通道，首次授权时自动完成 OAuth 客户端注册（RFC 7591），
  **不需要**到开放平台手动申请 client id / secret。
- `.env` 只需配置账号区域：`COROS_REGION=cn`（可选 `eu` / `us` / `auto`），
  回调地址固定为 `http://localhost:8000/api/connections/coros/callback`。
- 支持活动 + 身体数据（官方恢复状态写入当日 body metric）。
- 官方 MCP 不支持 webhook 推送，定时拉取是保持数据实时的方式。

## 佳明 Garmin（二选一）

1. **个人账号方式（默认）**：社区非官方库 `garminconnect`（`pip install garminconnect`），
   `.env` 填 `GARMIN_EMAIL / GARMIN_PASSWORD`。仅个人自用；官方改版端点可能变动，
   届时修改 `app/integrations/garmin.py`。
2. **官方 Health API**：需加入 Garmin 开发者计划，填 `GARMIN_CLIENT_ID / GARMIN_CLIENT_SECRET`。

佳明同步自动写入每日身体数据（HRV、睡眠、静息心率、体重体脂、血氧、呼吸率、压力、身体电量）。

## Strava（个人开发者免费申请）

1. 到 [strava.com/settings/api](https://www.strava.com/settings/api) 创建应用。
2. 回调地址填 `http://localhost:8000/api/connections/strava/callback`，
   `STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET` 填入 `.env`。
3. 推荐链路：高驰 App 开启「同步到 Strava」→ 本平台经 Strava API 拉取真实分段 /
   逐点曲线 / GPS 轨迹（高驰列表接口不含逐点数据）。

## 手动同步范围

首次连接或手动点同步时可选范围：近 90 天 / 半年 / 近一年；
长范围按 60 天分段滚动拉取，避免单次请求过大。

## 训练课表下发

- 佳明 / Strava：结构化训练（热身 / 间歇 / 配速心率区间）直接写入平台，
  App 同步后手表可开始；今日页课卡上的「下发」按钮操作。
- 高驰：MCP 暂未开放训练写入，只能导出通用 FIT 文件手动导入。

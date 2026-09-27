"""开发启动脚本：python run.py

⚠️ 绑定地址必须是 127.0.0.1，不要改成 0.0.0.0。
本平台所有接口均无鉴权（个人单用户自用设计），一旦监听全网卡，
同一局域网（如校园网）内任何人都能读取全部训练/身体数据并调用写接口。
桌面端 desktop.py 同样绑定 127.0.0.1，此处保持一致。
"""
import uvicorn
from app.main import app

if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, reload=False)
    server = uvicorn.Server(config)
    # 挂到 app.state 供 POST /api/app/shutdown 优雅停服（桌面壳 desktop.py 关窗时调用）
    app.state.uvicorn_server = server
    server.run()

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title RunOS

REM RunOS 一键启动（浏览器模式）：服务已在跑则直接开浏览器；
REM 首次运行自动安装后端依赖、构建前端。

curl -s -o nul -m 2 http://localhost:8000/ >nul 2>nul
if not errorlevel 1 (
  echo [RunOS] 服务已在运行，直接打开浏览器
  start "" http://localhost:8000
  exit /b 0
)

REM 优先 py 启动器（避开 Microsoft Store 的 python 别名占位）
set "PYCMD=python"
where py >nul 2>nul && set "PYCMD=py -3"
where python >nul 2>nul || set "PYCMD=py -3"

echo [RunOS] 检查后端依赖...
%PYCMD% -c "import fastapi" >nul 2>nul
if errorlevel 1 (
  echo [RunOS] 首次运行：安装后端依赖...
  %PYCMD% -m pip install -r backend\requirements.txt || (echo [RunOS] 依赖安装失败 & pause & exit /b 1)
)

if not exist "web\dist\index.html" (
  echo [RunOS] 首次运行：构建前端（约 1 分钟）...
  pushd web
  if not exist "node_modules" call npm install --registry=https://registry.npmmirror.com
  call npm run build
  if errorlevel 1 (echo [RunOS] 前端构建失败 & popd & pause & exit /b 1)
  popd
)

echo [RunOS] 启动服务...
cd backend
start "RunOS 后端服务" cmd /k "%PYCMD% run.py"
cd ..
echo [RunOS] 等服务就绪...
timeout /t 3 /nobreak >nul
start "" http://localhost:8000
echo [RunOS] 浏览器已打开 http://localhost:8000
echo [RunOS] 停止服务：关闭「RunOS 后端服务」窗口
timeout /t 5 >nul

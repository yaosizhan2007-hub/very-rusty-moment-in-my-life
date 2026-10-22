@echo off
rem Rusty Moment 一键启动（开发环境）：优先使用项目内 .venv
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py
) else (
    echo [提示] 未找到 .venv，请先运行 setup.bat 安装依赖
    pause
)
endlocal

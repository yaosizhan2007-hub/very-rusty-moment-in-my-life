@echo off
rem 创建虚拟环境并安装依赖（Python 3.11 ~ 3.13）
setlocal
cd /d "%~dp0"
if not exist ".venv" (
    python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
echo.
echo 安装完成。运行 run.bat 启动，或运行 pytest 执行测试。
goto :eof

:error
echo 安装失败，请检查 Python 是否已加入 PATH。
pause

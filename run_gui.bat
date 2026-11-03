@echo off
setlocal
cd /d "%~dp0"

rem sau GUI launcher. NOTE: must stay GBK + CRLF (cmd parses this file in cp936).

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto :have_py
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :have_py
)

echo [sau] 未找到可用的 Python。请安装官方 Python 3.9+，安装时勾选 "Add python.exe to PATH"。
echo        下载地址: https://www.python.org/downloads/
pause
exit /b 1

:have_py
echo [sau] 正在启动图形界面。本窗口用于显示日志，可最小化，请勿直接关闭（会连程序一起退出）。
%PY% -m sau gui 2>"%~dp0gui_error.log"
if errorlevel 1 (
    echo.
    echo [sau] 启动失败，详细错误见同目录 gui_error.log。可先运行诊断: %PY% -m sau doctor
    type "%~dp0gui_error.log"
    pause
)
endlocal

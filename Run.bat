@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE="
if exist "config.ini" for /f "tokens=1,* delims==" %%A in ('findstr /i /b "python_path" "config.ini"') do set "PYTHON_EXE=%%B"
if defined PYTHON_EXE for /f "tokens=*" %%A in ("%PYTHON_EXE%") do set "PYTHON_EXE=%%A"
if defined PYTHON_EXE goto configured

where py >nul 2>&1
if not errorlevel 1 goto launcher
where python >nul 2>&1
if not errorlevel 1 goto system_python
if exist "..\..\python_embeded\python.exe" goto embedded

echo 未找到 Python。请安装带有 Tkinter 的 Python，或在 config.ini 中设置 python_path。
pause
exit /b 1

:configured
"%PYTHON_EXE%" "%~dp0main.py"
goto finished

:launcher
py -3 "%~dp0main.py"
goto finished

:system_python
python "%~dp0main.py"
goto finished

:embedded
"..\..\python_embeded\python.exe" "%~dp0main.py"

:finished
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" exit /b 0
echo.
echo 启动或运行失败，请查看上方错误。如果提示缺少 tkinter，请使用带有 Tkinter 的 Python 启动 main.py。
pause
exit /b %RESULT%

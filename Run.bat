@echo off
setlocal enabledelayedexpansion

:: 设置配置文件名
set CONFIG_FILE=config.ini
set SCRIPT_FILE=main.py

:: 检查配置文件是否存在
if not exist "%CONFIG_FILE%" (
    echo [Error] 找不到 %CONFIG_FILE%
    echo 请确保配置文件存在。
    pause
    exit /b
)

:: 从 config.ini 中读取 python_path
:: 逻辑：寻找以 python_path 开头的行，提取等号后的内容
set "PYTHON_EXE="
for /f "tokens=1,* delims==" %%A in ('type "%CONFIG_FILE%" ^| findstr /i "^python_path"') do (
    set "KEY=%%A"
    set "VAL=%%B"
    :: 去除空格
    set "VAL=!VAL: =!"
    set "PYTHON_EXE=!VAL!"
)

:: 如果没读取到
if "%PYTHON_EXE%"=="" (
    echo [Error] 无法在 %CONFIG_FILE% 中找到 python_path 设置
    pause
    exit /b
)

:: 检查 Python 是否存在 (处理相对路径)
if not exist "%PYTHON_EXE%" (
    echo [Warning] 配置的路径 "%PYTHON_EXE%" 似乎不存在。
    echo 尝试直接运行...
)

echo 使用 Python: %PYTHON_EXE%
echo running...

:: 启动 Python 脚本
"%PYTHON_EXE%" "%SCRIPT_FILE%"

if %errorlevel% neq 0 (
    echo Error
    pause
)
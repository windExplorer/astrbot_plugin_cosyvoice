@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 让 Python 强制以 UTF-8 处理标准输入输出与文件系统编码
set "PYTHONUTF8=1"

echo.
echo ╔══════════════════════════════════════════════╗
echo ║        🎙  CosyVoice TTS API 启动器            ║
echo ╚══════════════════════════════════════════════╝
echo.

REM ============================================================
REM  按需修改下面的变量（改完直接双击本文件即可启动）
REM ============================================================
REM 模型目录或 ModelScope 仓库名（默认相对本脚本所在目录）
set "MODEL_DIR=pretrained_models\Fun-CosyVoice3-0.5B-2512"

REM 参考音频目录（放 wav 的地方，不存在会自动创建）
set "VOICES_DIR=%~dp0cosyvoice_voices"

REM 监听端口（默认 50000，如果和 Gradio WebUI 冲突请换一个）
set "PORT=50000"

REM ============================================================

REM 检查 uv
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 uv，请先安装 uv：
    echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo 或访问：https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

if not exist "%VOICES_DIR%" mkdir "%VOICES_DIR%"

REM 检查模型是否存在（不存在则首次运行会从 ModelScope 自动下载）
if not exist "%MODEL_DIR%\cosyvoice.yaml" (
    if not exist "%MODEL_DIR%\cosyvoice2.yaml" (
        if not exist "%MODEL_DIR%\cosyvoice3.yaml" (
            echo [警告] 模型目录未找到: %MODEL_DIR%
            echo         如果是首次运行，将自动从 ModelScope 下载...
            echo.
        )
    )
)

echo [信息] 模型目录: %MODEL_DIR%
echo [信息] 音频目录: %VOICES_DIR%
echo [信息] 监听端口: %PORT%
echo.
echo 正在加载模型，稍候...
echo.

uv run python cosyvoice_api.py --model_dir "%MODEL_DIR%" --voices_dir "%VOICES_DIR%" --port %PORT%

if errorlevel 1 (
    echo.
    echo [错误] API 服务异常退出，错误码: %errorlevel%
)
pause

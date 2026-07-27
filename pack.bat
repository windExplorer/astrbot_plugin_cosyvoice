@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo 正在打包插件（排除 .git / __pycache__ / 打包脚本等）...
echo.

python pack.py %*

echo.
pause

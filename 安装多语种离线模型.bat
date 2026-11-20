@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv\Scripts\python.exe，请先确认本工具已完整解压。
    pause
    exit /b 1
)
if not exist "models\opus-mt-en-zh-ct2\model.bin" (
    echo [X] 缺少英文离线模型 models\opus-mt-en-zh-ct2，请先补齐工具目录。
    pause
    exit /b 1
)
echo 正在检查多语种离线模型…
".venv\Scripts\python.exe" fetch_multilingual_model.py
if errorlevel 1 (
    echo.
    echo [X] 安装未完成，请检查网络后重新运行本脚本。
) else (
    echo.
    echo [OK] 多语种离线模型已就绪，保密模式可以翻译非英文文档。
)
pause
endlocal

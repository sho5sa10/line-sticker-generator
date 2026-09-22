@echo off
rem LINE sticker generator - GUI launcher (double-click to start)
chcp 65001 > nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 初回セットアップ中です（.venv を作成して必要なパッケージを入れます）...
    py -3.12 -m venv .venv 2> nul || python -m venv .venv
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :fail
)

echo GUI を起動します。ブラウザが自動で開きます。
echo 終了するときは、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じてください。
echo.
".venv\Scripts\python.exe" -m src.main gui %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo 起動に失敗しました。上のメッセージを確認してください。
pause
exit /b 1

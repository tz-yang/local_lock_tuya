@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo  推送到 GitHub: tz-yang/local_lock_tuya
echo ============================================
echo.

git status --short
echo.

git push -u origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送未完成，请检查网络或 GitHub 凭据。
) else (
    echo.
    echo [成功] 已推送到 origin/main。
)
echo.
pause

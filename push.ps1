Push-Location $PSScriptRoot
Write-Host "============================================"
Write-Host "  GitHub: tz-yang/local_lock_tuya"
Write-Host "============================================"
Write-Host ""
git status --short
Write-Host ""
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[失败] 推送未完成，请检查网络或 GitHub 凭据。" -ForegroundColor Red
} else {
    Write-Host ""
    Write-Host "[成功] 已推送到 origin/main。" -ForegroundColor Green
}
Write-Host ""
Pop-Location
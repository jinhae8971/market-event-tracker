# push.ps1 — 로컬 수정(events.json/tracker.py 등)을 레포에 반영 후 워크플로우 재실행
# 사용: $env:GH_TOKEN="ghp_..." 설정 후  .\push.ps1   (미설정 시 실행 중 입력 프롬프트)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding            = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

$GH_USER = "jinhae8971"
$GH_REPO = "market-event-tracker"
$GH_TOKEN = $env:GH_TOKEN
if (-not $GH_TOKEN) { $GH_TOKEN = Read-Host "GitHub Token 입력 (repo, workflow 스코프)" }

$REMOTE_URL = "https://$GH_TOKEN@github.com/$GH_USER/$GH_REPO.git"
$API_HDR    = @{ "Authorization"="token $GH_TOKEN"; "Accept"="application/vnd.github+json"; "User-Agent"="MET-Deploy" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
git config --global --add safe.directory ($ScriptDir -replace '\\','/') 2>$null
if (-not (Test-Path ".git")) { git init | Out-Null; git remote add origin $REMOTE_URL }
else { git remote set-url origin $REMOTE_URL 2>$null }
git config user.name $GH_USER; git config user.email "jinhae8971@gmail.com"

$ErrorActionPreference = "SilentlyContinue"
git add .
git commit -m "chore: update events/tracker" 2>$null
if ($LASTEXITCODE -ne 0) { git commit --allow-empty -m "chore: redeploy" 2>$null }
git branch -M main
git pull --rebase origin main 2>$null
git push -u origin main 2>$null
$code = $LASTEXITCODE; $ErrorActionPreference = "Stop"
if ($code -ne 0) { Write-Host "PUSH FAILED - 토큰 repo 스코프 확인" -ForegroundColor Red; exit 1 }
Write-Host "[OK] Pushed" -ForegroundColor Green

try {
    Invoke-RestMethod -Method Post `
        -Uri "https://api.github.com/repos/$GH_USER/$GH_REPO/actions/workflows/track.yml/dispatches" `
        -Headers $API_HDR -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
    Write-Host "[OK] Workflow triggered - 약 2분 후 대시보드 갱신" -ForegroundColor Green
} catch { Write-Host "수동 실행: https://github.com/$GH_USER/$GH_REPO/actions" -ForegroundColor White }

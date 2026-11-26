Set-Location $PSScriptRoot\..
Write-Host "Stopping Flagsmith containers..." -ForegroundColor Cyan
docker compose -f infra/docker-compose.yml down
Write-Host "Shutting down WSL2 background VM to reclaim RAM..." -ForegroundColor Cyan
wsl --shutdown
Write-Host "[OK] Stack stopped and memory reclaimed." -ForegroundColor Green

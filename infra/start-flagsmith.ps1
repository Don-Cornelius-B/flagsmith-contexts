Set-Location $PSScriptRoot\..

Write-Host "[1/3] Ensuring WSL2 Ubuntu engine stays awake..." -ForegroundColor Cyan
Start-Process wsl.exe -ArgumentList "-d Ubuntu -e sleep infinity" -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Host "[2/3] Starting Postgres + Flagsmith containers..." -ForegroundColor Cyan
docker compose -f infra/docker-compose.yml up -d

Write-Host "[3/3] Waiting for Flagsmith HTTP 200 at http://localhost:8000 ..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    $code = curl.exe -s -o NUL -w "%{http_code}" http://localhost:8000
    if ($code -eq "200") {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 2
}

if ($ready) {
    Write-Host "[OK] Flagsmith v2.294.0 is live at http://localhost:8000" -ForegroundColor Green
} else {
    Write-Host "[WARN] Timed out waiting for HTTP 200. Check: docker compose -f infra/docker-compose.yml logs --tail=30 flagsmith" -ForegroundColor Yellow
}

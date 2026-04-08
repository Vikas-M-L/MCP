# start.ps1 — clean-start PersonalOS Agent
# Kills any process holding ports 8000 or 8080, then starts main.py

$ports = @(8000, 8080)

foreach ($port in $ports) {
    $pids = netstat -ano | Select-String ":$port\s" | Select-String "LISTENING" |
            ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
    foreach ($p in $pids) {
        if ($p -match '^\d+$') {
            Write-Host "Killing PID $p (port $port)..." -ForegroundColor Yellow
            taskkill /F /PID $p 2>$null | Out-Null
        }
    }
}

Write-Host "Starting PersonalOS..." -ForegroundColor Green
python main.py

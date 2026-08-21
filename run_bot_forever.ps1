$ErrorActionPreference = "Continue"
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$mainPath = Join-Path $projectRoot "main.py"
$logPath = Join-Path $projectRoot "logs\bot_supervisor.log"

Set-Location -LiteralPath $projectRoot

while ($true) {
    $startedAt = Get-Date
    "[$startedAt] Starting TradingAI bot" | Out-File -FilePath $logPath -Append
    & $pythonPath $mainPath 2>&1 | Out-File -FilePath $logPath -Append
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 2) {
        "[$(Get-Date)] Another bot instance is active; supervisor exits" |
            Out-File -FilePath $logPath -Append
        break
    }

    "[$(Get-Date)] Bot exited with code $exitCode; restart in 10 seconds" |
        Out-File -FilePath $logPath -Append
    Start-Sleep -Seconds 10
}

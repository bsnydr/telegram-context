# Registers a Windows Scheduled Task that runs the bot at logon and restarts it
# if it stops: the Windows equivalent of the macOS launchd "KeepAlive" job.
#
# Run from PowerShell in this folder:   ./install-task.ps1
# (If blocked: powershell -ExecutionPolicy Bypass -File .\install-task.ps1)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "Telegram Context Bot"

$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
$script  = Join-Path $here "telegram_context.py"

if (-not (Test-Path $pythonw)) {
    Write-Host "ERROR: .venv not found. Run setup.bat first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $here ".env"))) {
    Write-Host "WARNING: .env not found. Copy .env.example to .env and add your token before the bot can work." -ForegroundColor Yellow
}

# pythonw.exe has no console window, so the bot runs silently in the background.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Telegram -> Claude context pipeline" -Force | Out-Null

Write-Host "Registered scheduled task '$taskName'. It starts automatically at your next logon." -ForegroundColor Green
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Watch the log:      Get-Content -Wait `"$here\logs\telegram-context.log`""

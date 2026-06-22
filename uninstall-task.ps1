# Stops and removes the scheduled task created by install-task.ps1.
#
# Run from PowerShell in this folder:   ./uninstall-task.ps1
# (If blocked: powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1)

$ErrorActionPreference = "Stop"
$taskName = "Telegram Context Bot"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'." -ForegroundColor Green
} else {
    Write-Host "Task '$taskName' is not installed."
}

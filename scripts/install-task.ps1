$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Run scripts\setup.ps1 first."
}
$action = New-ScheduledTaskAction -Execute $python -Argument "-m plex_queue" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "plex-queue" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "plex-queue will start when you sign in to Windows."

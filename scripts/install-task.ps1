$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# pythonw.exe = no bot console. Child tools (Hyper-V powershell, ssh, …) also use
# CREATE_NO_WINDOW in Python so they do not flash a black window at startup.
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    throw "Run scripts\setup.ps1 first (missing .venv\Scripts\pythonw.exe)."
}
# Observability (Grafana/Kafka/OTEL) is optional and separate:
#   cd observability; docker compose up -d
# Enable with otel.enabled in config.yaml after: pip install -e ".[otel]"
$action = New-ScheduledTaskAction -Execute $python -Argument "-m plex_queue" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
# Unlimited run time (constructor -ExecutionTimeLimit 0 can silently keep the 72h default).
$settings.ExecutionTimeLimit = "PT0S"
Register-ScheduledTask -TaskName "plex-queue" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "plex-queue will start when you sign in to Windows (no console window)."

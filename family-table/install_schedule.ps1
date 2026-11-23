# Idempotently installs or updates the daily Windows Scheduled Task for The Family Table.
# Runs daily at the configured -Time (default 08:00) and builds today's edition + Gmail draft.
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_schedule.ps1 [-Time '08:00']
param([string]$Time = '08:00')
$ErrorActionPreference = 'Stop'

$taskName = 'The Family Table - Daily Edition'
$runner = Join-Path $PSScriptRoot 'run.ps1'
if (-not (Test-Path $runner)) { throw "Missing runner script: $runner" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Today" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun:$false `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 3)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Generates today's 100% local 'The Family Table' broadsheet PDF (Pro + Gemma 4 Vision QA) and saves a ready-to-send Gmail draft." `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "Scheduled task ready: '$($t.TaskName)' | State=$($t.State) | Daily at $Time | NextRun=$($info.NextRunTime)"

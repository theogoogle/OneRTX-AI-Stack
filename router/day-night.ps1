# Awake hours schedule helper: holds ES_SYSTEM_REQUIRED during daytime hours and hibernates at night.
# (Hibernate S4 = fully off, no blinking power LED.)
#   -Mode day    started at wake time and at logon: holds ES_SYSTEM_REQUIRED until OffTime so Windows does not
#                auto-sleep during the day (especially the 2-minute unattended-wake sleep after an RTC wake).
#   -Mode night  started daily at OffTime: unloads GPU models (LM Studio, ComfyUI, voice-tts; they reload on the
#                next message, and freeing VRAM avoids fragile CUDA hibernate restores), releases the daytime
#                awake hold, and hibernates immediately. Motherboard BIOS RTC wakes the PC at WakeTime.
#   -WakeTime / -OffTime  optional 'HH:mm' overrides; defaults to 08:00-22:00 unless overridden in
#                         %USERPROFILE%\AI\secrets\day-night.json ({"wake_at":"08:00","off_at":"22:00"}).
#   -DryRun      checks and logs only, without unloading models or hibernating.
param(
    [ValidateSet('day', 'night')][string]$Mode = 'day',
    [string]$WakeTime = '',
    [string]$OffTime = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'day-night.log'
function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  [$Mode] $msg" | Out-File -FilePath $log -Append -Encoding utf8 }

$wakeStr, $offStr = '08:00', '22:00'
$cfgFile = Join-Path $env:USERPROFILE 'AI\secrets\day-night.json'
if (Test-Path -LiteralPath $cfgFile) {
    try {
        $cfg = Get-Content -LiteralPath $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.wake_at) { $wakeStr = [string]$cfg.wake_at }
        if ($cfg.off_at) { $offStr = [string]$cfg.off_at }
    } catch { }
}
if ($WakeTime) { $wakeStr = $WakeTime }
if ($OffTime) { $offStr = $OffTime }
$WakeAt, $OffAt = [timespan]$wakeStr, [timespan]$offStr
$KeepLoaded = @('lfm3-1.7b', 'qwen3.5-4b')        # CPU models (router labeller, Flash): stay loaded
$lms = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class Power {
    [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);
    [DllImport("powrprof.dll")] public static extern bool SetSuspendState(bool hibernate, bool forceCritical, bool disableWakeEvent);
}
'@
$ES_CONTINUOUS, $ES_SYSTEM_REQUIRED, $ES_DISPLAY_REQUIRED = [uint32]2147483648, [uint32]1, [uint32]2

if ($Mode -eq 'day') {
    $now = (Get-Date).TimeOfDay
    if ($now -lt $WakeAt.Subtract([timespan]'00:05') -or $now -ge $OffAt) {
        Log "outside awake hours ($((Get-Date).ToString('HH:mm'))), nothing to do"; exit 0
    }
    $mutex = New-Object System.Threading.Mutex($false, 'Local\AIPC-AwakeHours')
    if (-not $mutex.WaitOne(0)) { Log 'already running'; exit 0 }
    $until = (Get-Date).Date.Add($OffAt)
    Log "keeping the PC awake until $($until.ToString('HH:mm'))"
    if ($DryRun) { Log 'dry run: not holding'; exit 0 }
    [void][Power]::SetThreadExecutionState($ES_DISPLAY_REQUIRED)            # after an RTC wake: screen on once
    [void][Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)   # no sleep while this runs
    while ((Get-Date) -lt $until) { Start-Sleep -Seconds 60 }
    [void][Power]::SetThreadExecutionState($ES_CONTINUOUS)
    Log 'awake hours over'
    exit 0
}

# night: hibernate at configured OffAt every day (BIOS RTC alarm turns the PC back on at WakeAt)
Log "$($OffAt.ToString('hh\:mm')) hibernate starting (BIOS RTC wake at $($WakeAt.ToString('hh\:mm')))"
if ($DryRun) {
    $gpuLoaded = @()
    try {
        foreach ($m in (& $lms ps --json 2>$null | ConvertFrom-Json)) {
            if ($m.identifier -and $KeepLoaded -notcontains $m.identifier) { $gpuLoaded += $m.identifier }
        }
    } catch { }
    Log "dry run: would unload GPU models ($(if ($gpuLoaded) { $gpuLoaded -join ', ' } else { 'none loaded' })), free ComfyUI/TTS VRAM, stop -Mode day hold, and hibernate"
    exit 0
}

# 1. Free GPU VRAM first: LM Studio GPU models, ComfyUI, and voice-tts (all reload on the next message / image / narration)
try {
    $gone = @()
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # lms.exe prints status to stderr, which throws under 'Stop' in PS 5.1
    try {
        $psJson = & $lms ps --json 2>$null
        if ($psJson) {
            foreach ($m in ($psJson | ConvertFrom-Json)) {
                if ($m.identifier -and $KeepLoaded -notcontains $m.identifier) {
                    $null = & $lms unload $m.identifier 2>&1
                    $gone += $m.identifier
                }
            }
        }
    } finally {
        $ErrorActionPreference = $prevEap
    }
    Log "unloaded GPU models: $(if ($gone) { $gone -join ', ' } else { 'none loaded' })"
} catch { Log "could not unload GPU models: $_" }

try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8188/free -ContentType 'application/json' -Body '{"unload_models": true, "free_memory": true}' -TimeoutSec 15 | Out-Null } catch { }
try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8945/unload -ContentType 'application/json' -Body '{}' -TimeoutSec 10 | Out-Null } catch { }

# 2. End any daytime ES_SYSTEM_REQUIRED hold before calling SetSuspendState
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'day-night\.ps1' -and $_.CommandLine -match '-Mode day' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. Enter S4 Hibernate (disableWakeEvent = $false so ACPI keeps hardware/BIOS RTC wake enabled)
$ok = [Power]::SetSuspendState($true, $false, $false)
Log $(if ($ok) { 'resumed from hibernate' } else { 'hibernate FAILED (is hibernation enabled? powercfg /a)' })

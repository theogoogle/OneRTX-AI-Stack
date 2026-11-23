# Scheduled-task and CLI runner for the 100% local "The Family Table" pipeline.
# Safe to re-run: mutex prevents concurrent runs; updates existing Gmail draft in place.
# Usage:
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Today
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Tomorrow -NoDraft
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Date 2026-11-23
param(
    [switch]$Today,
    [switch]$Tomorrow,
    [string]$Date = '',
    [switch]$NoDraft,
    [switch]$NoMemory,
    [switch]$NoVision,
    [string]$Instructions = ''
)
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'family-table.log'
function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  [run.ps1] $msg" | Out-File -FilePath $log -Append -Encoding utf8 }

$mutex = New-Object System.Threading.Mutex($false, 'Local\FamilyTableRun')
if (-not $mutex.WaitOne(0)) {
    Log 'another Family Table run is already in progress; exiting'
    exit 0
}
try {
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 512KB) {
        $tail = Get-Content -LiteralPath $log -Tail 200 -Encoding utf8
        $tail | Set-Content -LiteralPath $log -Encoding utf8
    }
    $py = (Get-Command python -ErrorAction Stop).Source
    $argsList = @('-I', '-X', 'utf8', (Join-Path $PSScriptRoot 'pipeline.py'))
    if ($Today) { $argsList += '--today' }
    elseif ($Tomorrow) { $argsList += '--tomorrow' }
    elseif ($Date) { $argsList += $Date }
    if ($NoDraft) { $argsList += '--no-draft' }
    if ($NoMemory) { $argsList += '--no-memory' }
    if ($NoVision) { $argsList += '--no-vision' }
    if ($Instructions) { $argsList += @('--instructions', $Instructions) }

    Log ("starting: python " + ($argsList -join ' '))
    $out = & $py @argsList 2>&1
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Log 'completed successfully (exit 0)'
    } else {
        $snippet = ($out | Out-String)
        if ($snippet.Length -gt 600) { $snippet = $snippet.Substring(0, 600) }
        Log "failed (exit $code): $snippet"
    }
    $out
    exit $code
} finally {
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}

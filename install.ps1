<#
.SYNOPSIS
    Idempotent from-scratch installer for OneRTX-AI-Stack (Windows 11 / 12 GB consumer GPU).

.DESCRIPTION
    Checks and installs missing prerequisites via winget, deploys selected modules into an isolated
    or live directory (<InstallDir>\.lmstudio and <InstallDir>\AI), creates virtual environments,
    generates fresh local secrets, downloads selected GGUFs via LM Studio (`lms`), and prints exact next steps.
    Safe to run multiple times without overwriting user secrets or databases.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -WithPowerTools -WithCodingAgents -SkipModels
    .\install.ps1 -InstallDir "$env:TEMP\onertx-test" -WithPowerTools -SkipSystemInstalls -SkipModels -SkipScheduledTask -NonInteractive
#>
[CmdletBinding()]
param(
    [switch]$WithPowerTools,
    [switch]$WithCodingAgents,
    [switch]$WithComputerUse,
    [switch]$WithImages,
    [switch]$WithVoice,
    [switch]$WithBroadsheet,
    [switch]$All,
    [switch]$SkipModels,
    [switch]$SkipSystemInstalls,
    [switch]$SkipScheduledTask,
    [switch]$SkipWebUIPip,
    [switch]$CheckOnly,
    [switch]$NonInteractive,
    [string]$InstallDir = $env:USERPROFILE,
    [int]$RouterPort = 4000,
    [int]$WebUIPort = 8282,
    [string]$TailscaleHost = ""
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot

if ($All) {
    $WithPowerTools = $true
    $WithCodingAgents = $true
    $WithComputerUse = $true
    $WithImages = $true
    $WithVoice = $true
    $WithBroadsheet = $true
}

$anyFlag = $WithPowerTools -or $WithCodingAgents -or $WithComputerUse -or $WithImages -or $WithVoice -or $WithBroadsheet
if (-not $anyFlag -and -not $NonInteractive -and -not $CheckOnly) {
    Write-Host ""
    Write-Host "=== OneRTX-AI-Stack Installer ===" -ForegroundColor Cyan
    Write-Host "Core (Router :$RouterPort + LM Studio Presets + Open WebUI :$WebUIPort) is always installed."
    Write-Host "Optional modules:"
    Write-Host "  1) Power Tools (Slides, Deep Research, Finance, Smart Visuals, Word/PDF Export, YouTube, Workspace)"
    Write-Host "  2) Coding Agents (Open Terminal :8940, OpenCode/Claude Code configs, scene3d & pptx_studio MCPs)"
    Write-Host "  3) Computer Use (Playwright MCP Browser + NorthCUA Desktop Control presets)"
    Write-Host "  4) Images (ComfyUI + Qwen-Image-2.1 configs)"
    Write-Host "  5) Voice Studio (Qwen3-TTS + Chatterbox local podcast/voice-cloning server)"
    Write-Host "  6) Daily Broadsheet (Scheduled PDF newspaper + Google OAuth template)"
    Write-Host "  A) All modules"
    Write-Host "  0) Core only"
    $choice = Read-Host "Select modules (comma-separated, e.g. 1,2,3 or A) [1]"
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
    $tokens = ($choice.ToUpper() -split '[,\s]+' | Where-Object { $_ })
    if ($tokens -contains 'A') {
        $WithPowerTools = $WithCodingAgents = $WithComputerUse = $WithImages = $WithVoice = $WithBroadsheet = $true
    } else {
        if ($tokens -contains '1') { $WithPowerTools = $true }
        if ($tokens -contains '2') { $WithCodingAgents = $true }
        if ($tokens -contains '3') { $WithComputerUse = $true }
        if ($tokens -contains '4') { $WithImages = $true }
        if ($tokens -contains '5') { $WithVoice = $true }
        if ($tokens -contains '6') { $WithBroadsheet = $true }
    }
}

function Refresh-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $extra = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:USERPROFILE '.lmstudio\bin'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\Scripts'),
        'C:\Program Files\LibreOffice\program'
    )
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $merged = New-Object 'System.Collections.Generic.List[string]'
    foreach ($seg in (@($env:Path -split ';') + $extra + @($user -split ';') + @($machine -split ';'))) {
        $s = ($seg -replace '\\+$', '').Trim()
        if ($s -and $seen.Add($s)) { $merged.Add($s) }
    }
    $env:Path = ($merged -join ';')
}

function Find-Python312 {
    $cands = New-Object 'System.Collections.Generic.List[string]'
    if ($env:pythonLocation) { $cands.Add((Join-Path $env:pythonLocation 'python.exe')) }
    foreach ($cmdName in @('python3.12', 'python')) {
        Get-Command $cmdName -All -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.Source) { $cands.Add($_.Source) }
        }
    }
    $cands.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
    foreach ($cand in $cands) {
        if ($cand -and (Test-Path -LiteralPath $cand) -and ($cand -notmatch '\\WindowsApps\\')) {
            try {
                $ver = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($ver -match '^3\.(1[1-9]|2\d)') { return $cand }
            } catch { }
        }
    }
    return $null
}

function Ensure-WingetPackage([string]$WingetId, [string]$Label) {
    if ($CheckOnly) {
        Write-Host "  [MISSING] $Label ($WingetId)" -ForegroundColor Yellow
        return $false
    }
    if ($SkipSystemInstalls) {
        Write-Host "  [SKIP] $Label missing and -SkipSystemInstalls set ($WingetId)" -ForegroundColor Yellow
        return $false
    }
    $wg = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $wg) {
        throw "winget not found while trying to install $Label ($WingetId). Install it manually and re-run."
    }
    Write-Host "  Installing $Label via winget ($WingetId)..." -ForegroundColor Cyan
    & winget install --id $WingetId --exact --silent --accept-source-agreements --accept-package-agreements
    Refresh-SessionPath
    return $true
}

Refresh-SessionPath
Write-Host "`n[1/5] Checking prerequisites..." -ForegroundColor Green

$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
$pyExe = Find-Python312
$hasNode = [bool](Get-Command node -ErrorAction SilentlyContinue)
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
$lmsExe = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
$hasLms = Test-Path -LiteralPath $lmsExe
$hasFfmpeg = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)
$hasSoffice = (Test-Path 'C:\Program Files\LibreOffice\program\soffice.exe') -or [bool](Get-Command soffice -ErrorAction SilentlyContinue)

if ($hasGit) { Write-Host "  [OK] git" } else { Ensure-WingetPackage 'Git.Git' 'Git' | Out-Null; $hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue) }
if ($pyExe) { Write-Host "  [OK] python ($pyExe)" } else { Ensure-WingetPackage 'Python.Python.3.12' 'Python 3.12' | Out-Null; $pyExe = Find-Python312 }
if ($hasNode) { Write-Host "  [OK] node ($(node -v))" } else { Ensure-WingetPackage 'OpenJS.NodeJS.LTS' 'Node.js LTS' | Out-Null; $hasNode = [bool](Get-Command node -ErrorAction SilentlyContinue) }
if ($hasUv) { Write-Host "  [OK] uv" } else { Ensure-WingetPackage 'astral-sh.uv' 'uv (Python package manager)' | Out-Null; $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue) }
if ($hasLms) { Write-Host "  [OK] LM Studio CLI (lms.exe)" } else { Ensure-WingetPackage 'ElementLabs.LMStudio' 'LM Studio' | Out-Null; $hasLms = Test-Path -LiteralPath $lmsExe }

if ($WithVoice) {
    if ($hasFfmpeg) { Write-Host "  [OK] ffmpeg" } else { Ensure-WingetPackage 'Gyan.FFmpeg' 'FFmpeg' | Out-Null; $hasFfmpeg = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) }
}
if ($WithPowerTools -or $WithCodingAgents) {
    if ($hasSoffice) { Write-Host "  [OK] LibreOffice (optional PPTX QA)" } else { Ensure-WingetPackage 'TheDocumentFoundation.LibreOffice' 'LibreOffice' | Out-Null }
}

if ($CheckOnly) {
    Write-Host "`nCheckOnly complete (no files modified)." -ForegroundColor Cyan
    exit 0
}

if (-not $pyExe) {
    throw "Python 3.11+ is required and could not be located."
}

# Target paths (idempotent)
$targetLmsScripts = Join-Path $InstallDir '.lmstudio\scripts'
$targetLmsCfg     = Join-Path $InstallDir '.lmstudio\.internal\user-concrete-model-default-config'
$targetAi         = Join-Path $InstallDir 'AI'
$targetSecrets    = Join-Path $targetAi 'secrets'
$targetOwui       = Join-Path $targetAi 'open-webui'

foreach ($d in @($targetLmsScripts, (Join-Path $targetLmsScripts 'config-backups'), $targetLmsCfg, $targetAi, $targetSecrets, $targetOwui, (Join-Path $targetAi 'projects'), (Join-Path $targetAi 'generated'))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

function Copy-DirContents([string]$SrcRel, [string]$DestDir) {
    $src = Join-Path $RepoRoot $SrcRel
    if (-not (Test-Path -LiteralPath $src)) { return }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $DestDir -Recurse -Force
}

function Copy-FileIfExists([string]$SrcRel, [string]$DestFile) {
    $src = Join-Path $RepoRoot $SrcRel
    if (-not (Test-Path -LiteralPath $src)) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $DestFile) | Out-Null
    Copy-Item -LiteralPath $src -Destination $DestFile -Force
}

function New-SecretFileIfMissing([string]$Path, [int]$Bytes = 32) {
    if (Test-Path -LiteralPath $Path) { return }
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    $hex = -join ($buf | ForEach-Object { $_.ToString('x2') })
    Set-Content -LiteralPath $Path -Value $hex -NoNewline -Encoding ascii
    Write-Host "  Generated secret: $Path"
}

Write-Host "`n[2/5] Deploying files into $InstallDir..." -ForegroundColor Green
Copy-DirContents 'router' $targetLmsScripts
Copy-DirContents 'lmstudio-model-configs' $targetLmsCfg

foreach ($f in @('run.ps1', 'patch_agent_names.py', 'setup_backup.py', 'setup-snapshot.json')) {
    Copy-FileIfExists "open-webui\$f" (Join-Path $targetOwui $f)
}
New-SecretFileIfMissing (Join-Path $targetOwui '.webui_secret_key') 32

# Customize ports / Tailscale CORS in open-webui/run.ps1 cleanly if requested
$owuiRun = Join-Path $targetOwui 'run.ps1'
if (Test-Path -LiteralPath $owuiRun) {
    $runTxt = Get-Content -LiteralPath $owuiRun -Raw -Encoding utf8
    if ($TailscaleHost) {
        $runTxt = $runTxt -replace "(CORS_ALLOW_ORIGIN = '[^']+)'", "`$1;https://$TailscaleHost'"
    }
    if ($RouterPort -ne 4000) {
        $runTxt = $runTxt -replace 'http://127\.0\.0\.1:4000', "http://127.0.0.1:$RouterPort"
    }
    if ($WebUIPort -ne 8282) {
        $runTxt = $runTxt -replace '127\.0\.0\.1:8282', "127.0.0.1:$WebUIPort" -replace 'localhost:8282', "localhost:$WebUIPort" -replace '--port 8282', "--port $WebUIPort"
    }
    Set-Content -LiteralPath $owuiRun -Value $runTxt -Encoding utf8
}

# Regression tests (needed for verify.py --run-unit-tests)
Copy-DirContents 'benchmark' (Join-Path $InstallDir '.lmstudio\benchmark')

if ($WithPowerTools) {
    Copy-FileIfExists 'open-webui\install_generate_slides.py' (Join-Path $targetOwui 'install_generate_slides.py')
    Copy-DirContents 'open-webui\tools' (Join-Path $targetOwui 'tools')
    Copy-DirContents 'open-webui\functions' (Join-Path $targetOwui 'functions')
    foreach ($mod in @('deep-research', 'doc-export', 'smart-visuals', 'market-finance', 'pptx_studio')) {
        Copy-DirContents $mod (Join-Path $targetAi $mod)
    }
}

if ($WithCodingAgents) {
    $otDir = Join-Path $targetAi 'open-terminal'
    $ocCfgDir = Join-Path $InstallDir '.config\opencode'
    $ccCfgDir = Join-Path $otDir 'claude-config'
    New-Item -ItemType Directory -Force -Path $ocCfgDir, $ccCfgDir, (Join-Path $InstallDir '.local\bin') | Out-Null
    foreach ($f in @('run.ps1', 'patch_job_object.py', 'clear_stale_opencode.py', 'claude-shell.md', 'build-agent-instructions.ps1')) {
        Copy-FileIfExists "open-terminal\$f" (Join-Path $otDir $f)
    }
    foreach ($f in @('opencode.json', 'AGENTS.md', 'shell-powershell.md')) {
        Copy-FileIfExists "open-terminal\$f" (Join-Path $ocCfgDir $f)
    }
    Copy-FileIfExists 'open-terminal\claude-config-settings.json' (Join-Path $ccCfgDir 'settings.json')
    Copy-FileIfExists 'open-terminal\claude-config-CLAUDE.md' (Join-Path $ccCfgDir 'CLAUDE.md')
    Copy-FileIfExists 'claude-local\claude-local.cmd' (Join-Path $InstallDir '.local\bin\claude-local.cmd')
    Copy-DirContents 'scene3d' (Join-Path $targetAi 'scene3d')
    Copy-DirContents 'pptx_studio' (Join-Path $targetAi 'pptx_studio')
    New-SecretFileIfMissing (Join-Path $targetSecrets 'open_terminal_api_key.txt') 24
}

if ($WithComputerUse) {
    Copy-DirContents 'playwright-mcp' (Join-Path $targetAi 'playwright-mcp')
    Copy-DirContents 'northcua-harness' (Join-Path $targetAi 'northcua-harness\logs\my-config')
}

if ($WithImages) {
    Copy-DirContents 'comfyui' (Join-Path $targetAi 'ComfyUI')
}

if ($WithVoice) {
    Copy-DirContents 'voice-tts' (Join-Path $targetAi 'voice-tts')
}

if ($WithBroadsheet) {
    Copy-DirContents 'family-table' (Join-Path $targetAi 'family-table')
    New-Item -ItemType Directory -Force -Path (Join-Path $targetSecrets 'family_table') | Out-Null
}

Write-Host "`n[3/5] Setting up Python & Node virtual environments..." -ForegroundColor Green
$owuiVenv = Join-Path $targetOwui '.venv'
$owuiPy = Join-Path $owuiVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $owuiPy)) {
    Write-Host "  Creating Open WebUI venv..."
    if ($hasUv) {
        & uv venv --python $pyExe $owuiVenv
    } else {
        & $pyExe -m venv $owuiVenv
    }
} else {
    Write-Host "  [OK] Open WebUI venv exists"
}

function Install-PyPackages([string]$VenvPy, [string[]]$Packages) {
    if (-not $Packages -or $Packages.Count -eq 0) { return }
    if ($hasUv) {
        & uv pip install --python $VenvPy @Packages
    } else {
        & $VenvPy -m pip install @Packages
    }
}

if (-not $SkipWebUIPip) {
    if (-not (Test-Path -LiteralPath (Join-Path $owuiVenv 'Scripts\open-webui.exe'))) {
        Write-Host "  Installing open-webui into .venv (this takes a few minutes on first run)..."
        Install-PyPackages $owuiPy @('open-webui')
    } else {
        Write-Host "  [OK] open-webui already installed in .venv"
    }
} else {
    Write-Host "  [SKIP] open-webui pip install (-SkipWebUIPip)"
}

if ($WithPowerTools) {
    Write-Host "  Ensuring Power Tools libraries..."
    $ptPkgs = @('python-pptx', 'matplotlib', 'yfinance', 'youtube-transcript-api', 'python-docx', 'pillow', 'beautifulsoup4', 'markdown', 'lxml', 'pyyaml', 'mcp', 'playwright')
    Install-PyPackages $owuiPy $ptPkgs
}

if ($WithComputerUse -and $hasNode) {
    $pwDir = Join-Path $targetAi 'playwright-mcp'
    if ((Test-Path (Join-Path $pwDir 'package.json')) -and -not (Test-Path (Join-Path $pwDir 'node_modules'))) {
        Write-Host "  Installing playwright-mcp npm dependencies..."
        Push-Location $pwDir
        try { & npm ci } finally { Pop-Location }
    }
}

Write-Host "`n[4/5] Models & Startup Watchdog..." -ForegroundColor Green
$manifestPath = Join-Path $RepoRoot 'models.json'
if (-not $SkipModels -and $hasLms -and (Test-Path -LiteralPath $manifestPath)) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    $wantedMods = @('core')
    if ($WithComputerUse) { $wantedMods += 'computer_use' }
    if ($WithCodingAgents) { $wantedMods += 'coding_agents' }
    $installedIds = @()
    try {
        $lsRaw = & $lmsExe ls --json 2>$null | ConvertFrom-Json
        $installedIds = @($lsRaw | ForEach-Object { $_.modelKey })
    } catch { }
    foreach ($m in $manifest.models) {
        if ($wantedMods -notcontains $m.module) { continue }
        if ($installedIds -contains $m.id) {
            Write-Host "  [OK] Model already downloaded: $($m.id)"
        } else {
            Write-Host "  Downloading $($m.id) ($($m.hf_repo))..."
            & $lmsExe get "$($m.hf_repo)" -y
        }
    }
} else {
    Write-Host "  [SKIP] Model downloads skipped (-SkipModels or lms.exe absent). See models.json for exact GGUFs."
}

$normInstall = ([IO.Path]::GetFullPath($InstallDir)).TrimEnd('\')
$normUser    = ([IO.Path]::GetFullPath($env:USERPROFILE)).TrimEnd('\')
if (-not $SkipScheduledTask -and ($normInstall -ieq $normUser)) {
    $taskName = 'LM Studio - start LLM stack'
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        $startScript = Join-Path $targetLmsScripts 'start-llm-stack.ps1'
        $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`""
        $t1 = New-ScheduledTaskTrigger -AtLogOn
        $t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $act -Trigger @($t1, $t2) -Settings $set -Force | Out-Null
        Write-Host "  [OK] Registered scheduled startup task: $taskName"
    } else {
        Write-Host "  [OK] Scheduled task already exists: $taskName"
    }
} else {
    Write-Host "  [SKIP] Scheduled task registration skipped (-SkipScheduledTask or non-default -InstallDir)"
}

Write-Host "`n[5/5] Installation complete!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Optional Web Search: put your free Tavily API key in $(Join-Path $targetSecrets 'tavily_api_key.txt')"
Write-Host "  2. Verify offline/mock: python -I -X utf8 $(Join-Path $RepoRoot 'verify.py') --install-dir `"$InstallDir`" --router-port $RouterPort --mock-lmstudio"
Write-Host "  3. Start stack (when installed to %USERPROFILE%): powershell -ExecutionPolicy Bypass -File $(Join-Path $targetLmsScripts 'start-llm-stack.ps1')"
Write-Host "  4. Health check: http://127.0.0.1:$RouterPort/health | Open WebUI: http://127.0.0.1:$WebUIPort"

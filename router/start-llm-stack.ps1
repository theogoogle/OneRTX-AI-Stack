# Starts the LM Studio headless service, the API server, the always-on router model, the router,
# ComfyUI (images) and Open WebUI (chat app).
# Safe to run any number of times: each step is skipped if it is already done.
# Log: %USERPROFILE%\.lmstudio\scripts\start-llm-stack.log

$ErrorActionPreference = 'Continue'
$lms = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
$log = Join-Path $PSScriptRoot 'start-llm-stack.log'

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $log -Append -Encoding utf8 }

# Is something accepting connections on this port? A real connect, not Get-NetTCPConnection: after the reboots of
# 2026-11-08 00:10-00:12, Windows reported most listening sockets with no state (netstat did not list them either),
# so every check failed and each 10-minute run started another router (31 copies sharing :4000) and restarted the
# LM Studio server (which kills requests in progress).
function Test-Port([int]$port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { return $c.ConnectAsync('127.0.0.1', $port).Wait(2000) -and $c.Connected } catch { return $false } finally { $c.Dispose() }
}

# One run at a time (at logon the logon trigger and the 10-minute trigger can fire together: 00:13:48 and 00:13:53)
$mutex = New-Object System.Threading.Mutex($false, 'Local\LLMStackStart')
if (-not $mutex.WaitOne(0)) { exit 0 }

# Keep the log short, and prune run/server/browser logs older than 7 days
if ((Test-Path $log) -and (Get-Item $log).Length -gt 200KB) { Remove-Item $log -Force }
Log '--- start'
try {
    $cutoff = (Get-Date).AddDays(-7)
    $cutoffDateStr = $cutoff.ToString('yyyy-MM-dd')
    $aiRoot = Join-Path $env:USERPROFILE 'AI'
    # 1. NorthCUA Harness run folders (task-*, web-*, tutorial) older than 7 days; never touch my-config
    $ncLogs = Join-Path $aiRoot 'northcua-harness\logs'
    if (Test-Path $ncLogs) {
        Get-ChildItem -LiteralPath $ncLogs -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^(task-|web-|tutorial$)' -and $_.LastWriteTime -lt $cutoff } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
    # 2. LM Studio daily server-logs older than 7 days (+ empty month folders)
    $srvLogs = Join-Path $env:USERPROFILE '.lmstudio\server-logs'
    if (Test-Path $srvLogs) {
        Get-ChildItem -LiteralPath $srvLogs -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        Get-ChildItem -LiteralPath $srvLogs -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue) } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
    }
    # 3. Playwright MCP output artifacts older than 7 days
    foreach ($pwOut in @('playwright-mcp\output', 'playwright-mcp\output-headed')) {
        $pDir = Join-Path $aiRoot $pwOut
        if (Test-Path $pDir) {
            Get-ChildItem -LiteralPath $pDir -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt $cutoff } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        }
    }
    # 4. router.log: trim lines with a yyyy-MM-dd prefix older than 7 days (checked once when file > 2 MB or oldest line < cutoff)
    $rLog = Join-Path $PSScriptRoot 'router.log'
    if (Test-Path $rLog) {
        $firstLine = Get-Content -LiteralPath $rLog -TotalCount 1 -ErrorAction SilentlyContinue
        if ($firstLine -and $firstLine -match '^(\d{4}-\d\d-\d\d)' -and $Matches[1] -lt $cutoffDateStr) {
            $kept = Get-Content -LiteralPath $rLog -ErrorAction SilentlyContinue |
                Where-Object { -not ($_ -match '^(\d{4}-\d\d-\d\d)' -and $Matches[1] -lt $cutoffDateStr) }
            $kept | Set-Content -LiteralPath $rLog -Encoding utf8
        }
    }
} catch { Log "log-cleanup warning: $_" }

# 1. Headless service (no-op if the LM Studio app or daemon is already running)
& $lms daemon up 2>&1 | ForEach-Object { Log "daemon: $_" }

# 2. API server on 127.0.0.1:1234. Only start it when it is not already listening:
#    "lms server start" restarts a running server, which kills requests and model loads in progress.
for ($i = 1; $i -le 10; $i++) {
    if (Test-Port 1234) {
        Log 'server: already listening on :1234'
        break
    }
    & $lms server start --port 1234 2>&1 | ForEach-Object { Log "server: $_" }
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 3
}

# 3. Router model: CPU only, 16K context (room for MCP tool definitions when chatting in the app),
#    no TTL (manual loads are never auto-evicted)
$loaded = @()
try { $loaded = & $lms ps --json 2>$null | ConvertFrom-Json } catch { }
if ($loaded | Where-Object { $_.identifier -eq 'lfm3-1.7b' }) {
    Log 'router: lfm3-1.7b already loaded'
} else {
    & $lms load lfm3-1.7b --gpu off --context-length 16384 --identifier lfm3-1.7b -y 2>&1 |
        Where-Object { $_ -notmatch 'Loading .*%' } | ForEach-Object { Log "router: $_" }
}

# 3b. Flash model (Qwen3.5-4B UD-Q4_K_XL): CPU only, 32K context, always loaded. Answers when picked as "Flash", and
#     everyday questions while another model is mid-task on the GPU. `lms load` takes keys, not paths (a path fails);
#     the exact key loads the right file. The router's keeper also reloads it every minute if it goes missing.
if ($loaded | Where-Object { $_.identifier -eq 'qwen3.5-4b' }) {
    Log 'flash: qwen3.5-4b already loaded'
} else {
    & $lms load qwen3.5-4b --gpu off --context-length 32768 --identifier qwen3.5-4b -y 2>&1 |
        Where-Object { $_ -notmatch 'Loading .*%' } | ForEach-Object { Log "flash: $_" }
}

# 4. Router for Claude Code on 127.0.0.1:4000 (router.py next to this script, runs hidden with pythonw)
if (Test-Port 4000) {
    Log 'router: already listening on :4000'
} else {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    $pyw = if ($py) { Join-Path (Split-Path $py) 'pythonw.exe' } else { $null }
    if ($pyw -and (Test-Path $pyw)) {
        Start-Process -FilePath $pyw -ArgumentList '-I', "`"$(Join-Path $PSScriptRoot 'router.py')`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
        Log "router: started with $pyw"
    } else {
        Log 'router: pythonw.exe not found, router not started'
    }
}

# 5. ComfyUI on 127.0.0.1:8188 (image generation behind the router's /v1/images/generations)
$ai = Join-Path $env:USERPROFILE 'AI'
if (Test-Port 8188) {
    Log 'comfyui: already listening on :8188'
} elseif (Test-Path (Join-Path $ai 'ComfyUI\run.ps1')) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$(Join-Path $ai 'ComfyUI\run.ps1')`"" -WindowStyle Hidden
    Log 'comfyui: started'
}

# 5b. Playwright MCP, the "Browser" tools in Open WebUI (the router sends these loops to NorthCUA):
#     :8931 "Browser" (visible Chrome window) and :8932 "Browser (headless)"
foreach ($b in @(@{Port = 8931; Mode = 'headed'}, @{Port = 8932; Mode = 'headless'})) {
    if (Test-Port $b.Port) {
        Log "browser ($($b.Mode)): already listening on :$($b.Port)"
    } elseif (Test-Path (Join-Path $ai 'playwright-mcp\run.ps1')) {
        Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', "`"$(Join-Path $ai 'playwright-mcp\run.ps1')`"", '-Mode', $b.Mode -WindowStyle Hidden
        Log "browser ($($b.Mode)): started"
    }
}
# keep-alive: Playwright closes its browser when the last client leaves (Open WebUI leaves after every message)
if (-not (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" | Where-Object { $_.CommandLine -match 'keepalive\.py' })) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    $pyw = if ($py) { Join-Path (Split-Path $py) 'pythonw.exe' } else { $null }
    if ($pyw -and (Test-Path $pyw)) {
        Start-Process -FilePath $pyw -ArgumentList '-I', "`"$(Join-Path $ai 'playwright-mcp\keepalive.py')`"" -WindowStyle Hidden
        Log 'browser keep-alive: started'
    }
} else { Log 'browser keep-alive: already running' }

# Clear stale suspended OpenCode sessions when idle so interrupted tasks never replay across UIs
$sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
$clearOc = Join-Path $ai 'open-terminal\clear_stale_opencode.py'
if ($sysPy -and (Test-Path $clearOc)) {
    try {
        $ocMsg = & $sysPy -I -X utf8 $clearOc 2>&1 | Select-Object -Last 1
        if ($ocMsg -and $ocMsg -notmatch '^0 stale') { Log "opencode-cleanup: $ocMsg" }
    } catch { }
}

# 5c. Open Terminal on 127.0.0.1:8940: coding agents (OpenCode, Claude Code) as models in Open WebUI, on local models
if (Test-Port 8940) {
    Log 'open-terminal: already listening on :8940'
} elseif (Test-Path (Join-Path $ai 'open-terminal\run.ps1')) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$(Join-Path $ai 'open-terminal\run.ps1')`"" -WindowStyle Hidden
    Log 'open-terminal: started'
}

# 5d. Podcast Voice-Cloning Studio & TTS server on 127.0.0.1:8945 (Qwen3-TTS + Chatterbox Multilingual)
if (Test-Port 8945) {
    Log 'voice-tts: already listening on :8945'
} elseif (Test-Path (Join-Path $ai 'voice-tts\run.ps1')) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$(Join-Path $ai 'voice-tts\run.ps1')`"" -WindowStyle Hidden
    Log 'voice-tts: started'
}

# 6. Open WebUI on 127.0.0.1:8282 (8080 and 8090 are used by other apps on this PC) (chat app for the phone, through Tailscale)
if (Test-Port 8282) {
    Log 'open-webui: already listening on :8282'
} elseif (Test-Path (Join-Path $ai 'open-webui\run.ps1')) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', "`"$(Join-Path $ai 'open-webui\run.ps1')`"" -WindowStyle Hidden
    Log 'open-webui: started'
}

# 7. Token ledger -> Open WebUI Analytics (2026-11-10): rewrites the archived "Token ledger <month> (router)" chats from
#    token-usage.csv (token_openwebui.py: fixed ids, safe to re-run, only today's numbers change)
try {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py -and (Test-Path (Join-Path $PSScriptRoot 'token_openwebui.py'))) {
        $out = & $py -I (Join-Path $PSScriptRoot 'token_openwebui.py') 2>&1 | Select-Object -Last 1
        Log "token ledger -> Open WebUI: $out"
    }
} catch { Log "token ledger -> Open WebUI warning: $_" }

Log '--- done'

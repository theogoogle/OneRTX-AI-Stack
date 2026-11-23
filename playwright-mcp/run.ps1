# Starts a Playwright MCP server (browser control for Open WebUI; the router sends these loops to NorthCUA).
# Started by %USERPROFILE%\.lmstudio\scripts\start-llm-stack.ps1, once per mode:
#   -Mode headed    "Browser"             http://127.0.0.1:8931/mcp  visible Chrome window on the PC (watch it live)
#   -Mode headless  "Browser (headless)"  http://127.0.0.1:8932/mcp  no window
# Both: Chrome with its own profile (none of your logins or cookies; one profile per mode, Chrome cannot share one),
# viewport 1280x800, --caps vision (coordinate tools for NorthCUA), --shared-browser-context (same tabs across chat
# messages; Open WebUI reconnects on every message).
param([ValidateSet('headed', 'headless')][string]$Mode = 'headed')
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ai = Split-Path -Parent $here
if ($Mode -eq 'headless') { $port = 8932; $profile = 'playwright-profile-headless'; $extra = @('--headless') }
else                      { $port = 8931; $profile = 'playwright-profile';          $extra = @() }
$log = Join-Path $ai "playwright-mcp-$Mode.log"
Set-Location $here
& (Join-Path $here 'node_modules\.bin\playwright-mcp.cmd') `
    --host 127.0.0.1 --port $port --allowed-hosts "127.0.0.1:$port,localhost:$port" `
    --browser chrome @extra `
    --user-data-dir (Join-Path $ai $profile) `
    --shared-browser-context `
    --caps vision `
    --viewport-size 1280x800 `
    --output-dir (Join-Path $here "output-$Mode") `
    2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $log -Append -Encoding utf8

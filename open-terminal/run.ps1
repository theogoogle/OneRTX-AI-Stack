# Open Terminal on 127.0.0.1:8940: lets Open WebUI use coding agents as models (OpenCode + Claude Code), both on the
# local models through the router. Started by start-llm-stack.ps1. The API key stays in AI\secrets (never in git).
# Agents work in AI\projects; OpenCode's permissions (~\.config\opencode\opencode.json) deny files outside its
# project and ask before shell commands; Claude Code writes only inside its folder and asks before commands.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here 'open-terminal.log'
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item $log "$log.old" -Force }
$env:OPEN_TERMINAL_API_KEY = (Get-Content (Join-Path $env:USERPROFILE 'AI\secrets\open_terminal_api_key.txt') -Raw).Trim()
# OpenCode: the CLI bundled with the OpenCode desktop app (not on PATH)
$env:OPEN_TERMINAL_OPENCODE_COMMAND = Join-Path $env:LOCALAPPDATA 'Programs\ai.opencode.desktop\resources\opencode-cli.exe'
# Codex is installed and signed in to an OpenAI account on this PC: hidden here, so Open WebUI's model picker only
# offers local models (a missing path makes Open Terminal report it as not installed)
$env:OPEN_TERMINAL_CODEX_COMMAND = Join-Path $here 'codex-disabled.exe'
# Claude Code on the local router: the same settings as claude-local.cmd
$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:4000'
$env:ANTHROPIC_AUTH_TOKEN = 'local'
$env:ANTHROPIC_MODEL = 'local-auto'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = 'local-auto'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = 'local-auto'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = 'local-background'
$env:CLAUDE_CODE_SUBAGENT_MODEL = 'local-auto'
$env:CLAUDE_CODE_MAX_CONTEXT_TOKENS = '131072'
$env:CLAUDE_CODE_AUTO_COMPACT_WINDOW = '112000'
$env:CLAUDE_CODE_MAX_OUTPUT_TOKENS = '32768'
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
# One entry in Open WebUI's model picker: its own settings folder (availableModels = default only; the user's normal
# Claude Code keeps ~\.claude), and no 1M-context variant (router overflows >45K to Pro 256K automatically)
$env:CLAUDE_CONFIG_DIR = Join-Path $here 'claude-config'
$env:CLAUDE_CODE_DISABLE_1M_CONTEXT = '1'
# make_3d_scene (AI\scene3d) takes 3-10 minutes: allow tool calls up to 30 minutes
$env:MCP_TOOL_TIMEOUT = '1800000'
$projects = Join-Path $env:USERPROFILE 'AI\projects'
New-Item -ItemType Directory -Force $projects | Out-Null
$otPy = Join-Path $env:APPDATA 'uv\tools\open-terminal\Scripts\python.exe'
if (Test-Path $otPy) {
    & $otPy -I -X utf8 (Join-Path $here 'patch_job_object.py') 2>&1 |
        ForEach-Object { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $_" } | Out-File -FilePath $log -Append -Encoding utf8
}
$sysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
$clearOc = Join-Path $here 'clear_stale_opencode.py'
if ($sysPy -and (Test-Path $clearOc)) {
    try {
        & $sysPy -I -X utf8 $clearOc 2>&1 |
            ForEach-Object { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $_" } | Out-File -FilePath $log -Append -Encoding utf8
    } catch { }
}
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  starting open-terminal on 127.0.0.1:8940, cwd $projects" | Out-File $log -Append -Encoding utf8
& (Join-Path $env:USERPROFILE '.local\bin\open-terminal.exe') run --host 127.0.0.1 --port 8940 --cwd $projects `
    --cors-allowed-origins 'http://127.0.0.1:8282,http://localhost:8282,https://<pc-name>.<tailnet>.ts.net' 2>&1 |
    ForEach-Object { "$_" } | Out-File -FilePath $log -Append -Encoding utf8

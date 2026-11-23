# Starts Open WebUI on 127.0.0.1:8282, talking to the local router (chat + images). Started by
# %USERPROFILE%\.lmstudio\scripts\start-llm-stack.ps1. Reach it from your phone through Tailscale (tailscale serve).
# These values are the initial settings; once Open WebUI has saved them, its Admin Settings page is the place to change them.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:DATA_DIR = Join-Path $here 'data'
$env:WEBUI_SECRET_KEY = (Get-Content (Join-Path $here '.webui_secret_key') -Raw).Trim()
$env:WEBUI_NAME = 'Local AI'
# Security review 2026-11-08: only Open WebUI's own addresses may call its API from a browser (default was '*')
$env:CORS_ALLOW_ORIGIN = 'http://127.0.0.1:8282;http://localhost:8282;https://<pc-name>.<tailnet>.ts.net'
# Security review 2026-11-21: disable community sharing UI, version update checks, and outbound telemetry
$env:ENABLE_COMMUNITY_SHARING = 'false'
$env:ENABLE_VERSION_UPDATE_CHECK = 'false'
$env:SCARF_NO_ANALYTICS = 'true'
$env:DO_NOT_TRACK = 'true'
$env:ANONYMIZED_TELEMETRY = 'false'
$env:ENABLE_OLLAMA_API = 'false'
$env:OPENAI_API_BASE_URL = 'http://127.0.0.1:4000/v1'
$env:OPENAI_API_KEY = 'local'
$env:DEFAULT_MODELS = 'auto'                     # the router picks Gemma / Qwen3.6 / Qwen3.8 / NorthCUA per message
$env:TASK_MODEL_EXTERNAL = 'background'          # titles and tags: whichever model is already loaded, no swap
$env:ENABLE_EVALUATION_ARENA_MODELS = 'false'
$env:ENABLE_IMAGE_GENERATION = 'true'
$env:IMAGE_GENERATION_ENGINE = 'openai'           # OpenAI-format images endpoint = the router -> ComfyUI
$env:IMAGES_OPENAI_API_BASE_URL = 'http://127.0.0.1:4000/v1'
$env:IMAGES_OPENAI_API_KEY = 'local'
$env:IMAGE_GENERATION_MODEL = 'qwen-image-2.1'
$env:IMAGE_SIZE = '1024x1024'
$env:ENABLE_IMAGE_PROMPT_GENERATION = 'false'     # Qwen-Image's own prompt enhancer is used instead when enabled
# Web Search switch (+ menu): searches go through the router (Tavily key lives there, every search is logged).
# These only seed the first start; afterwards the values live in data\webui.db (Admin Panel > Settings > Web Search).
$env:ENABLE_WEB_SEARCH = 'true'
$env:WEB_SEARCH_ENGINE = 'external'
$env:EXTERNAL_WEB_SEARCH_URL = 'http://127.0.0.1:4000/v1/web/search'
$env:EXTERNAL_WEB_SEARCH_API_KEY = 'local'
$env:WEB_SEARCH_RESULT_COUNT = '5'
$env:PYTHONIOENCODING = 'utf-8'
$log = Join-Path $here 'open-webui.log'
# Friendly coding-agent names in the model picker ("🧑‍💻 Open Code", "🤖 Claude Code"); re-applied after updates
& (Join-Path $here '.venv\Scripts\python.exe') -I -X utf8 (Join-Path $here 'patch_agent_names.py') 2>&1 |
    ForEach-Object { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $_" } | Out-File -FilePath $log -Append -Encoding utf8
& (Join-Path $here '.venv\Scripts\open-webui.exe') serve --host 127.0.0.1 --port 8282 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $log -Append -Encoding utf8

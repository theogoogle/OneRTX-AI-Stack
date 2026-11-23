# Starts ComfyUI on 127.0.0.1:8188 (image generation for the router). Started by
# %USERPROFILE%\.lmstudio\scripts\start-llm-stack.ps1. Its own web UI: http://127.0.0.1:8188
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path (Split-Path -Parent $here) 'comfyui.log'
Set-Location $here
& (Join-Path $here '.venv\Scripts\python.exe') main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $log -Append -Encoding utf8

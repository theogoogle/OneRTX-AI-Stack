# Starts the local Podcast Voice-Cloning Studio & TTS server on 127.0.0.1:8945.
# Started by %USERPROFILE%\.lmstudio\scripts\start-llm-stack.ps1. Studio UI: http://127.0.0.1:8945
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$pyw = if ($py) { Join-Path (Split-Path $py) 'pythonw.exe' } else { $null }
$exe = if ($pyw -and (Test-Path $pyw)) { $pyw } elseif ($py) { $py } else { Join-Path $here '.venv-qwen\Scripts\python.exe' }
Start-Process -FilePath $exe -ArgumentList '-I', "`"$(Join-Path $here 'server.py')`"" -WorkingDirectory $here -WindowStyle Hidden

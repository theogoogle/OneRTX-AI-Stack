# One-time (or re-auth) Google OAuth 2.0 Desktop App authorization for The Family Table.
# Usage:
#   powershell -ExecutionPolicy Bypass -File auth_setup.ps1           # authorize in browser
#   powershell -ExecutionPolicy Bypass -File auth_setup.ps1 -Check    # verify existing token
#   powershell -ExecutionPolicy Bypass -File auth_setup.ps1 -Reauth   # force fresh consent
param([switch]$Check, [switch]$Reauth)
$ErrorActionPreference = 'Stop'
$py = (Get-Command python -ErrorAction Stop).Source
$argsList = @('-I', '-X', 'utf8', (Join-Path $PSScriptRoot 'auth_setup.py'))
if ($Check) { $argsList += '--check' }
if ($Reauth) { $argsList += '--reauth' }
& $py @argsList
exit $LASTEXITCODE

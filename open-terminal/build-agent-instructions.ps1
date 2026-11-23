# Builds the Claude Code copy of the global coding-agent instructions. Idempotent: safe to re-run.
#
# Sources (edit these, never the built copies):
#   %USERPROFILE%\.config\opencode\AGENTS.md            shared core (OpenCode + Claude Code)
#   %USERPROFILE%\.config\opencode\shell-powershell.md  OpenCode only (its shell tool is Windows PowerShell 5.1);
#                                                       loaded through opencode.json "instructions"
#   %USERPROFILE%\AI\open-terminal\claude-shell.md      Claude Code only (its Bash tool is Git Bash)
# Built copies:
#   AI\open-terminal\claude-config\CLAUDE.md   = core + claude-shell.md (Open WebUI Claude Code via CLAUDE_CONFIG_DIR;
#                                                 claude-local.cmd appends the same file)
#   AI\scene3d\AGENT-INSTRUCTIONS.md           = core (reference copy)
# Hard rule: the model-facing text must never contain the word for the denied OpenCode tool (it primed a 164-call loop).
$ErrorActionPreference = 'Stop'
$u = $env:USERPROFILE
$utf8 = New-Object System.Text.UTF8Encoding $false
$core   = [IO.File]::ReadAllText("$u\.config\opencode\AGENTS.md", $utf8).TrimEnd()
$psSh   = [IO.File]::ReadAllText("$u\.config\opencode\shell-powershell.md", $utf8).TrimEnd()
$bashSh = [IO.File]::ReadAllText("$u\AI\open-terminal\claude-shell.md", $utf8).TrimEnd()

$forbidden = 'sk' + 'ill'
foreach ($pair in @(@('AGENTS.md', $core), @('shell-powershell.md', $psSh), @('claude-shell.md', $bashSh))) {
    if ($pair[1] -match "(?i)$forbidden") { throw "$($pair[0]) contains the forbidden word; refusing to build." }
}

$targets = [ordered]@{
    "$u\AI\open-terminal\claude-config\CLAUDE.md" = "$core`n`n$bashSh`n"
    "$u\AI\scene3d\AGENT-INSTRUCTIONS.md"         = "$core`n"
}
foreach ($t in $targets.Keys) {
    $new = $targets[$t]
    $old = if (Test-Path $t) { [IO.File]::ReadAllText($t, $utf8) } else { '' }
    if ($old -ne $new) { [IO.File]::WriteAllText($t, $new, $utf8); "updated   $t" } else { "unchanged $t" }
}
$lines = @{ OpenCode = ("$core`n`n$psSh" -split "`n").Count; ClaudeCode = ("$core`n`n$bashSh" -split "`n").Count }
"lines seen by the model: OpenCode $($lines.OpenCode), Claude Code $($lines.ClaudeCode)"

## Shell: Windows PowerShell 5.1
- Run checks as `node .agent-check/check.js; Remove-Item .agent-check/check.js` (`;` so the file is removed even if the check fails; same for `.py`). No `&&` / `||`: use `;` or `if ($?) { ... }`. `%LOCALAPPDATA%\Temp\opencode` is NOT allowed, whatever the environment notes say.
- Never write JSON or code files with PowerShell `>`, `>>`, or `Out-File -Encoding utf8` (PS 5.1 writes UTF-16LE `0xFF 0xFE` or UTF-8 BOM `0xEF 0xBB 0xBF`, breaking strict UTF-8 parsers). Write files via Python (`encoding='utf-8'`), Node (`'utf8'`), or `[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))`.
- Call MCP tools from code mode: `await tools.scene3d.make_3d_scene({ description, file })`.

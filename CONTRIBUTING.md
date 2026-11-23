# Contributing to OneRTX-AI-Stack

Thanks for helping improve **OneRTX-AI-Stack**!

## Development Workflow
1. Fork this repo and clone it locally.
2. Make changes inside a test/sandbox install first using:
   ```powershell
   .\install.ps1 -InstallDir "$env:TEMP\onertx-dev" -WithPowerTools -SkipModels -SkipScheduledTask -RouterPort 14000 -WebUIPort 18282
   python -I -X utf8 verify.py --install-dir "$env:TEMP\onertx-dev" --router-port 14000 --mock-lmstudio --run-unit-tests
   ```
3. Run your installer a **second time** against the same `-InstallDir` to confirm idempotency.
4. Keep core `router/` modules Python 3.12 stdlib-only unless adding an optional sidecar module.
5. **Never commit API keys, OAuth tokens, real email addresses, personal file paths, or `.db`/log files.**

## Pull Request Checklist
- All Python files pass `py_compile`.
- `verify.py --mock-lmstudio --run-unit-tests` passes with zero errors.
- No personal data or secret prefixes (`sk-...`, `tvly-...`, `AIza...`, `ghp_...`) appear in any diff.

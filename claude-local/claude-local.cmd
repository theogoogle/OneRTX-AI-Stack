@echo off
rem Claude Code on local models: LM Studio (Qwen3.8-27B IQ3_S, 64K, with Qwen3.6-35B-A3B Pro fallback for >64K) through the router on 127.0.0.1:4000.
rem Plain "claude" is unaffected and keeps using your Anthropic account.
rem In a session:  /model local-auto   router decides: coding -> Qwen3.8 IQ3_S thinking on, everyday questions -> Gemma
rem                /model local-code   Qwen3.8 IQ3_S, never think   /model local-think  Qwen3.8 IQ3_S, always think
rem                /model local-deep   Pro (Qwen3.6-35B-A3B, 256K)  /model local-fast   Flash (GPU) (Gemma 4)
rem                /model local-3d     3D Coder (Three.js)          /model local-screen NorthCUA-Nano (screenshots)
rem                local-fast / local-3d / local-screen are not tuned for Claude Code's tools: expect rough edges.
rem Session size: 64K for Qwen3.8-27B IQ3_S (auto-compact at 56K; router falls back to Qwen3.6 256K if >64K).
setlocal
set "ANTHROPIC_BASE_URL=http://127.0.0.1:4000"
set "ANTHROPIC_AUTH_TOKEN=local"
set "ANTHROPIC_MODEL=local-auto"
set "ANTHROPIC_DEFAULT_OPUS_MODEL=local-auto"
set "ANTHROPIC_DEFAULT_SONNET_MODEL=local-auto"
set "ANTHROPIC_DEFAULT_HAIKU_MODEL=local-background"
set "CLAUDE_CODE_SUBAGENT_MODEL=local-auto"
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS=131072"
set "CLAUDE_CODE_AUTO_COMPACT_WINDOW=112000"
set "CLAUDE_CODE_MAX_OUTPUT_TOKENS=32768"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
rem Instructions: shared core + Git Bash section, built by AI\open-terminal\build-agent-instructions.ps1
claude --append-system-prompt-file "%USERPROFILE%\AI\open-terminal\claude-config\CLAUDE.md" %*

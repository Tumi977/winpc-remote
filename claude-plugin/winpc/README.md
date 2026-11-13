# winpc plugin

Lets Claude Code on a remote Linux server see and operate your local Windows PC through `winpc-agent` (which you run on Windows separately): screenshots, clicks, typing, key presses, file reads/writes and binary file transfer.

This directory contains:

- `.mcp.json`: connects Claude Code to the agent. The token and URL are read from the environment variables `WINPC_TOKEN` / `WINPC_URL`; nothing is written to a file;
- `skills/windows-pc-control/SKILL.md`: teaches the model to use the tools safely (look before acting, pass `expect_app` on every key press, paste long text, ask before risky actions);
- `scripts/winpc_cp.py`: a command-line tool for transferring files between Linux and Windows (standard library only; content never passes through the conversation; chunked, sha256-verified and resumable).

Install:

```bash
claude plugin marketplace add Tumi977/winpc-remote
claude plugin install winpc@winpc-remote
```

For installation, Windows setup, the SSH reverse tunnel, the security model and known risks, see the repository [README](../../README.md). Please read its "Security model and known risks" chapter before use: this lets a model operate your desktop as you.

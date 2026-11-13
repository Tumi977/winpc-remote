# winpc 插件

让远程 Linux 上的 Claude Code 通过 `winpc-agent`（需另行在 Windows 上运行）查看并操作你本地的 Windows 电脑：截图、点击、输入、按键、读写文件、传输二进制文件。

本目录包含：

- `.mcp.json`：把 Claude Code 连到 Agent。令牌和地址从环境变量 `WINPC_TOKEN` / `WINPC_URL` 读取，不写入任何文件；
- `skills/windows-pc-control/SKILL.md`：教模型安全地使用这些工具（先看后动、每次发键带 `expect_app`、长文本用粘贴、危险动作先问用户）；
- `scripts/winpc_cp.py`：Linux 与 Windows 之间传输文件的命令行工具（仅标准库，内容不经过对话，分块 + sha256 校验 + 断点续传）。

安装：

```bash
claude plugin marketplace add YOUR_GITHUB_USER/winpc-remote
claude plugin install winpc@winpc-remote
```

**安装、Windows 端配置、SSH 反向隧道、安全模型与已知风险**见仓库根目录的 [README](../../README.md)。使用前请务必先读其中的“安全模型与已知风险”：这会让模型以你的身份操作你的桌面。

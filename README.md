# WinPC Remote

让**远程 Linux 服务器上的 Claude Code**，通过「连续截屏 + 键鼠 + 文件读写」操作**你本地的 Windows 电脑**。

- **Windows 端**：常驻 Agent（`winpc-agent`），以 MCP（Streamable HTTP）暴露截图、点击、输入、按键、文件读写等工具。
- **Linux 端**：Claude Code 插件 `winpc`（MCP 配置 + Skill），经 **SSH 反向隧道**（或 Tailscale）连到 Agent。

Claude Desktop 自带的 computer use 没有对外接口，远程 Claude Code 无法借用；本项目补上“Windows 端执行器 + 安全通道”这一块。

> **这是第三方社区项目，与 Anthropic 无隶属关系。**

> ## ⚠️ 先读这段
> 装上它，就等于**让一个会被屏幕内容影响的模型，在你的桌面上以你的身份操作键盘和鼠标**。
> 本项目提供了令牌、应用授权、敏感路径拦截、紧急停止、审计日志等多层防护，但它**不是沙箱**。
> 请务必读完 [安全模型与已知风险](#六安全模型与已知风险)，尤其是 `bypass` 模式和“应用授权不等于沙箱”两节。
> **强烈建议**在专用 Windows 账户或虚拟机里运行。

---

## 目录

1. [它是怎么工作的](#一它是怎么工作的)
2. [快速开始（5 步）](#二快速开始5-步)
3. [Windows 端配置](#三windows-端配置)
4. [网络连通](#四网络连通)
5. [Linux 端配置](#五linux-端配置)
6. [安全模型与已知风险](#六安全模型与已知风险)
7. [工具一览与使用](#七工具一览与使用)
8. [测试](#八测试)
9. [故障排查](#九故障排查)
10. [升级、轮换令牌与卸载](#十升级轮换令牌与卸载)
11. [项目结构、变更与许可](#十一项目结构变更与许可)

---

## 一、它是怎么工作的

```
┌──────────────── Linux 服务器 ────────────────┐          ┌──────────────── 本地 Windows（已登录、未锁屏）───────────────┐
│  Claude Code                                 │          │  winpc-agent（Python，运行在当前用户桌面会话）               │
│   ├─ 插件 winpc                              │          │   ├─ MCP Server (Streamable HTTP) 127.0.0.1:8765/mcp          │
│   │   ├─ .mcp.json  → http://127.0.0.1:8765  │   SSH    │   ├─ Bearer 令牌校验 + Host/Origin 校验                       │
│   │   └─ skills/windows-pc-control/SKILL.md  │◄═反向隧道═│   ├─ 应用授权（弹窗 / 白名单 / 黑名单）                       │
│   └─ 模型：看截图、决定动作                  │   (-R)   │   ├─ 截图：mss → 缩放 → JPEG                                  │
│  127.0.0.1:8765 ← 隧道出口（只在服务器本地） │          │   ├─ 输入：SendInput（Unicode 安全）                          │
└──────────────────────────────────────────────┘          │   ├─ 紧急停止：鼠标甩到主屏左上角 → 暂停                     │
                                                          │   └─ 审计日志 logs/actions.jsonl                              │
                                                          └──────────────────────────────────────────────────────────────┘
```

连接是**由 Windows 主动发起**的：Windows 上的 `ssh -R` 把 Agent 端口映射到服务器的 `127.0.0.1`，所以 Windows 在 NAT 后面也能用，Agent 本身不对任何网络开放。

### 前置条件

| | 要求 |
|---|---|
| Windows | Windows 10/11，**已登录的交互式桌面会话**（不能是服务、SSH 会话；锁屏后不可用），Python 3.10+，OpenSSH 客户端（「设置 → 可选功能」里安装） |
| Linux | 能运行 Claude Code 的服务器，OpenSSH 服务端（`sshd`，建议 OpenSSH ≥ 7.8，下文受限密钥写法需要它） |
| 其他 | 一个能让 Windows 通过 SSH 连到服务器的网络路径（服务器有公网 IP / 跳板均可） |

---

## 二、快速开始（5 步）

```text
① Windows：  克隆仓库，运行 windows-agent\start-agent.ps1  → 记下首次打印的令牌
② 服务器：   创建一个无特权的隧道专用用户，放入受限 SSH 公钥            （第四章）
③ Windows：  配置 win-remote.config.json，运行 win-remote-on             （第三、四章）
④ 服务器：   把令牌写进 ~/.claude/settings.json 的 env，安装插件          （第五章）
⑤ Claude Code：  /mcp 看到 winpc 已连接 → “用 winpc 的 get_status 看一下电脑状态”
```

没有 Windows、只想试试 Linux 端？见 [八、测试](#八测试) 里的 `--backend fake`。

---

## 三、Windows 端配置

### 3.1 获取代码并启动

```powershell
git clone https://github.com/Tumi977/winpc-remote.git "$HOME\winpc-remote"
cd "$HOME\winpc-remote\windows-agent"
powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
```

> 建议把项目放在**你的用户目录**下（如上），不要放在 `D:\`、`E:\` 等根目录：这些位置的默认权限可能让同机其他账户读到 `config.json`。`start-agent.ps1` 也会收紧该文件的权限（见 3.2）。

`start-agent.ps1` 会依次：检测 Python 3.10+ → 创建 `.venv` → 安装依赖 → 从 `config.example.json` 生成 `config.json` → 收紧 `config.json` 权限 → 启动 Agent。

**首次启动会生成访问令牌，并且只在控制台打印这一次**（同时以明文保存在 `config.json`）。把它复制下来，第五章要用。

> 令牌 = 完全控制你电脑的钥匙。不要发给任何人，不要提交到 Git，不要贴进 Issue。
> 也可以用环境变量 `WINPC_TOKEN` 指定令牌，它优先于配置文件。

### 3.2 配置项（`config.json`）

`config.json` 由 `config.example.json` 复制而来，已被 `.gitignore` 忽略。Agent 通过 `--config` 参数也可以读取别处的配置文件。

| 项 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8765` | 只监听本机回环。用 Tailscale 时改成 Tailscale 网卡 IP。**不要**绑定 `0.0.0.0` 或局域网地址 |
| `token` | 自动生成 | Bearer 令牌 |
| `allowed_hosts` | `[]` | 额外允许的 Host 头（`127.0.0.1:*`、`localhost:*` 已内置；绑定到非回环 IP 时自动放行该 IP） |
| `approval_mode` | `prompt` | `prompt`：未授权应用/文件首次操作时在本机弹置顶确认框；`allowlist`：非白名单直接拒绝；`all`：全部放行（**不推荐**，见 6.2） |
| `approval_timeout_s` | `60` | 弹窗超时，超时视为拒绝 |
| `allowed_apps` | `["notepad.exe"]` | 白名单（进程名，不区分大小写，`.exe` 可省略） |
| `blocked_apps` | 密码管理器等 | 黑名单，优先级最高，任何模式都拒绝。**设置它会覆盖默认列表**，请把默认项一并写上 |
| `max_long_edge` | `1280` | 截图长边像素。约 1000–1500 token/张，可调到 1568 |
| `jpeg_quality` / `settle_ms` | `70` / `600` | JPEG 质量 / 动作后等多久再截图 |
| `log_dir` / `save_screenshots` | `logs` / `false` | 审计日志目录（相对 config 所在目录）/ 是否保存每张截图（截图含屏幕上的一切，默认关） |
| `file_tools_enabled` | `true` | 设为 `false` 则不注册 4 个文件工具 |
| `file_roots` | 桌面、文档（含 OneDrive 重定向） | 其中的**读取**免确认；**写入始终需要授权**。支持 `~` |
| `file_blocked_paths` | `[]` | 在内置敏感路径之外追加的路径子串（小写、正斜杠） |
| `type_chunk_chars` / `type_delay_ms` | `1` / `5` | `type_text` 的节奏：每几个字符一次 `SendInput`、批间隔多少毫秒。默认逐字符 + 5ms（约 200 字符/秒）。个别应用（如新版记事本）吃不消快速输入会丢字，若仍有问题就调大 `type_delay_ms` |
| `type_newline_delay_ms` | `150` | 回车后额外等待多久。回车会让部分应用（如新版记事本）卡顿，期间堆积的按键会被合并成同一个字符 |
| `paste_restore_delay_ms` | `600` | `type_text(method="paste")` 粘贴后等待多久再恢复原剪贴板（要等应用真正读走） |
| `transfer_max_bytes` | `536870912`（512 MiB） | 单个文件的传输上限（`get_file` / `put_file`） |
| `file_blocked_write_ext` | `.exe .bat .ps1 .lnk .reg .jse .url …` | 禁止写入的扩展名（可自动执行的内容）。**设置它会覆盖默认列表** |

**`config.json` 的权限**：令牌是明文的，所以 `start-agent.ps1` 每次启动都会用 `icacls` 取消继承，只授予当前用户读写。若你不用该脚本，请自行执行：

```powershell
icacls .\config.json /inheritance:r /grant:r "$([Security.Principal.WindowsIdentity]::GetCurrent().Name):(R,W)"
```

**内置的敏感路径与文件**（读写都拒绝，**任何授权模式（含 `all`/bypass）都生效**）：

- 目录：`.ssh`、`.aws`、`.gnupg`、`.azure`、`.kube`、`.docker`、`.config/gh`、`.config/gcloud`、`.claude`、Windows 凭据与 DPAPI 目录、Chrome / Edge / Firefox / Brave / Opera / Vivaldi 用户数据、PowerShell 命令历史、启动文件夹；
- 文件名：`.env`、`.netrc`、`.npmrc`、`.pypirc`、`.git-credentials`、`credentials.json`、`ntuser.dat`、`id_rsa*` / `id_ed25519*` 等、`*.kdbx` `*.pfx` `*.p12` `*.ppk`；
- **文件内容是私钥的文件**（含 `-----BEGIN … PRIVATE KEY-----` 或 PuTTY 私钥头）：不论放在哪里、叫什么名字都拒绝读取和修改；
- **Agent 自身目录、日志目录，以及你用 `--config` 指定的配置文件**（里面有令牌）。

这是尽力而为的**黑名单**，不是保证：它挡不住你放在任意位置、用不常见格式保存的密钥。真正重要的凭据请放进密码管理器。

### 3.3 开机自启

Agent **不能做成 Windows 服务**（服务运行在 session 0，看不到用户桌面）。用“登录时运行”的计划任务：

```powershell
schtasks /Create /TN WinPCAgent /SC ONLOGON /RL LIMITED /TR "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Users\YOU\winpc-remote\windows-agent\start-agent.ps1"
```

后台运行时看不到首次生成的令牌，所以**请先手动启动一次**并记下令牌。

### 3.4 一键启停（Agent + 隧道）

`windows-agent\bin\` 里有三个命令（把该目录加入 `PATH` 后可在任意位置使用）：

| 命令 | 作用 |
|---|---|
| `win-remote-on` | 启动 Agent 和隧道（各开一个窗口，请保持打开）；未授权应用会弹确认框 |
| `win-remote-bypass` | 同上，但 Agent 以 `--bypass-permissions` 启动：**跳过应用与文件授权确认**，黑名单和敏感路径拦截仍生效。慎用，见 6.2 |
| `win-remote-off` | 停止 Agent 与隧道 |

它们读取 `windows-agent\win-remote.config.json`（已被 `.gitignore` 忽略）。首次使用：

```powershell
cd "$HOME\winpc-remote\windows-agent"
Copy-Item win-remote.config.example.json win-remote.config.json
notepad win-remote.config.json
```

```json
{
  "server": "your.server.example.com",
  "user": "winpc-tunnel",
  "key_file": "C:\\Users\\YOU\\.ssh\\winpc_tunnel_ed25519",
  "ssh_port": 22,
  "port": 8765
}
```

---

## 四、网络连通

### 方案 A（推荐）：SSH 反向隧道

Agent 只监听 Windows 的 `127.0.0.1`，不暴露到任何网络；隧道全程加密；Windows 在 NAT 后面也能用。

#### 4.1 在 Windows 上生成**专用**密钥

不要复用你登录服务器的主密钥。放进 `%USERPROFILE%\.ssh\`（该目录被 Agent 的文件工具拦截）：

```powershell
ssh-keygen -t ed25519 -f "$HOME\.ssh\winpc_tunnel_ed25519" -C "winpc-tunnel"
# 公钥：$HOME\.ssh\winpc_tunnel_ed25519.pub  （只把 .pub 拷到服务器）
```

> 如果你把私钥放在 `.ssh` 之外（例如 `C:\Users\me\ssh\xxx`），Agent 也会因为文件内容是私钥而拒绝读取；但把它放在 `.ssh` 里仍是更稳妥的做法。

#### 4.2 在服务器上创建隧道专用用户，并限制这把密钥

隧道只需要“在服务器的 `127.0.0.1:8765` 上监听”这一件事，不需要 shell，更不需要 root。用一个无特权用户，并把公钥限制到只能做这件事：

```bash
sudo useradd -m -s /usr/sbin/nologin winpc-tunnel
sudo install -d -m 700 -o winpc-tunnel -g winpc-tunnel /home/winpc-tunnel/.ssh
```

把下面**一整行**写入 `/home/winpc-tunnel/.ssh/authorized_keys`（把末尾换成你的 `.pub` 内容），权限 600、属主 `winpc-tunnel`：

```text
restrict,port-forwarding,permitlisten="127.0.0.1:8765",permitopen="127.0.0.1:1",command="/bin/echo tunnel-only" ssh-ed25519 AAAA…你的公钥… winpc-tunnel
```

各选项的作用（已在 OpenSSH 8.9 上逐条实测）：

| 选项 | 效果 |
|---|---|
| `restrict` | 关闭 pty、agent / X11 转发、用户 rc 等一切默认能力 |
| `port-forwarding` | 重新开启端口转发（隧道需要） |
| `permitlisten="127.0.0.1:8765"` | 只允许 `-R` 监听这一个地址:端口；请求别的远程端口会被拒绝 |
| `permitopen="127.0.0.1:1"` | 把 `-L` 本地转发限制到一个无用的目标，等于禁用 |
| `command="…"` | 强制命令。`restrict` **不会**阻止执行命令，所以必须有它：即使这把密钥泄露，攻击者也只能得到一行 `tunnel-only` |

实测结果：`-R 127.0.0.1:8765` 通过并能转发流量；`-R` 其他端口被拒（`remote port forwarding failed`）；`ssh host 'id'` 只输出 `tunnel-only`；`-L` 被拒。

服务器 `sshd_config` 里确认没有禁用转发（默认就是允许）：

```text
AllowTcpForwarding yes      # 不能是 no
GatewayPorts no             # 默认值；保持 no，让转发端口只绑回环
```

> **同一台服务器上的其他用户**也能连到 `127.0.0.1:8765`，所以令牌是必须的，不是可选项。

#### 4.3 在 Windows 上启动隧道

推荐用 3.4 的 `win-remote-on`。也可以单独运行：

```powershell
.\start-tunnel.ps1 -Server your.server.example.com -User winpc-tunnel -KeyFile $HOME\.ssh\winpc_tunnel_ed25519
# 可选：-Port 8765 -RemotePort 8765 -SshPort 22
```

脚本带 `ServerAliveInterval=30`、`ExitOnForwardFailure=yes`、`IdentitiesOnly=yes`，断线 5 秒后自动重连；首次连接用 `StrictHostKeyChecking=accept-new`（首次信任，之后校验主机密钥）。

在服务器上验证隧道已通（**期望 401**，说明隧道通、Agent 在、令牌校验生效）：

```bash
curl -i http://127.0.0.1:8765/mcp
```

### 方案 B：Tailscale / ZeroTier

1. Windows 与 Linux 都加入同一个 tailnet。
2. `config.json` 里设 `"host": "100.x.y.z"`（Windows 的 Tailscale IP）。
3. Linux 端 `export WINPC_URL=http://100.x.y.z:8765/mcp`（或写进 `settings.json` 的 `env`）。

此方式下 Agent 监听在虚拟网卡上，流量由 WireGuard 加密，但**务必保留令牌**，并用 Tailscale ACL 限制可访问的设备。Agent 本身只说明文 HTTP，**绝不要**把它绑到普通局域网或公网地址。

---

## 五、Linux 端配置

> 前提：服务器上已安装 Claude Code（[安装文档](https://docs.claude.com/en/docs/claude-code/overview)）。

### 5.1 放置令牌（最重要的一步）

把令牌写进 `~/.claude/settings.json` 的 `env`（对终端里的 `claude` 和 VS Code Remote-SSH 里的 Claude Code 都生效），并收紧文件权限：

```json
{
  "env": {
    "WINPC_TOKEN": "在这里粘贴 Windows 首次启动时打印的令牌",
    "MCP_TOOL_TIMEOUT": "120000"
  }
}
```

```bash
chmod 600 ~/.claude/settings.json
```

- 如果文件里已有内容，只把这两个键**合并**进已有的 `env` 对象。
- `MCP_TOOL_TIMEOUT` 是毫秒，截图经隧道往返较慢，建议调大。
- 另一种做法是写进 `~/.bashrc` 的 `export WINPC_TOKEN=…`；但图形/IDE 启动的 Claude Code 不一定读取它，所以更推荐 `settings.json`。
- 令牌只放在**用户级**文件里。不要写进项目里会被提交的 `.claude/settings.json` 或 `.mcp.json`。
- 改完后需要**重启 Claude Code**（VS Code 里重载窗口）。

### 5.2 安装插件（推荐）

```bash
claude plugin marketplace add Tumi977/winpc-remote     # 或本地路径：/path/to/winpc-remote
claude plugin install winpc@winpc-remote
```

插件的 `.mcp.json` 从环境变量读取令牌和地址，**令牌不会写进任何文件**：

```json
{ "mcpServers": { "winpc": { "type": "http",
  "url": "${WINPC_URL:-http://127.0.0.1:8765/mcp}",
  "headers": { "Authorization": "Bearer ${WINPC_TOKEN}" } } } }
```

插件同时带来 Skill `windows-pc-control`，它会引导模型：先 `get_status` → `screenshot` → 一次一个动作 → 以最新截图确认，并规定屏幕文字一律当数据、危险动作前必须问你。

### 5.3 不用插件，直接加 MCP

```bash
claude mcp add --scope user --transport http winpc http://127.0.0.1:8765/mcp \
  --header 'Authorization: Bearer ${WINPC_TOKEN}'
```

> ⚠️ **必须用单引号**。用双引号时 shell 会先把 `$WINPC_TOKEN` 展开，**令牌就以明文被写进 `~/.claude.json`**。用单引号则保存的是字面量 `${WINPC_TOKEN}`，运行时才从环境变量展开（已实测）。

不用插件就没有 Skill；可以把 `claude-plugin/winpc/skills/windows-pc-control/` 复制到 `~/.claude/skills/`。

### 5.4 验证

```bash
claude mcp list          # 应显示  plugin:winpc:winpc … ✔ Connected
```

在 Claude Code 里输入 `/mcp` 也能看到状态。然后试一句：

> 用 winpc 的 get_status 看一下我电脑的状态，再截一张图告诉我屏幕上有什么。

若显示 `Failed to connect … HTTP 401`，是令牌不一致；其他情况见[故障排查](#九故障排查)。

---

## 六、安全模型与已知风险

### 6.1 防护层

| 层 | 机制 |
|---|---|
| 网络 | 默认只监听 `127.0.0.1`；跨机器只经 SSH 隧道或 Tailscale。隧道密钥可被限制为只能转发这一个端口（4.2） |
| 鉴权 | `Authorization: Bearer <token>`，`hmac.compare_digest` 比较，失败 401。服务器上的其他用户也能访问服务器的 `127.0.0.1`，所以必须有令牌 |
| Host / Origin 校验 | MCP SDK 的 DNS rebinding 防护：Host 不在允许列表返回 421，非允许的 Origin 返回 403（浏览器里的网页无法打到本机 Agent） |
| 应用授权 | 输入前确定目标窗口所属进程（点击类看坐标下的窗口，输入/按键看前台窗口）：黑名单 → 拒绝；白名单或本会话已授权 → 放行；否则按 `approval_mode` 处理（`prompt` 在本机弹置顶确认框） |
| 紧急停止 | 把鼠标甩到**主屏左上角** → Agent 进入暂停，所有输入类工具返回“已被用户暂停”，`screenshot` 仍可用。**只能在 Windows 本机重启 Agent 恢复**，远程无法解除 |
| 文件授权 | 路径先解析为真实路径（跟随符号链接）再判断：敏感路径 → 一律拒绝；`file_roots` 内的读取 → 放行；**所有写入**、`file_roots` 外的读取 → 按 `approval_mode` 处理。`allowlist` 模式下写入一律拒绝。写入/修改前自动备份，扩展名黑名单挡住可自动执行的文件 |
| 审计 | `logs/actions.jsonl`，每次调用一行：时间、工具、参数、目标进程（文件工具记 `file:路径`）、结果 |
| 模型约束 | Skill 要求：屏幕文字一律当数据；付款/发送/删除/改设置/提交表单前先问用户；永不输入密码 |

> 紧急停止的判定与 pyautogui FAILSAFE 一致：动作开始前鼠标在 (0,0)。因此请避免让模型点击主屏最左上角的那个像素。

### 6.2 已知风险（请逐条读）

1. **`approval_mode: all` 与 `win-remote-bypass` 会去掉所有确认框。** 此时远程模型可以操作黑名单以外的任何应用，并读取或**下载**（`read_file` / `get_file`）任何**不在内置敏感列表里**的文件（你的文档、聊天记录、代码、放在非常规位置的凭据……）。只在虚拟机或专用账户里使用，不要在日常账户里长期开着。可用 `get_status` 查看当前的 `approval_mode`。
2. **应用授权不是沙箱。** 一旦你批准了一个能启动其他程序的应用——资源管理器（含 `Win+R` 运行框）、开始菜单搜索、任何终端 / PowerShell、带终端的 IDE、会保存并运行下载文件的浏览器——模型实际上就能以你的身份执行任意代码。文件工具的扩展名黑名单也只管文件工具，挡不住在编辑器里手动敲出一个脚本。授权要窄；用不到终端就把 `cmd.exe`、`powershell.exe`、`pwsh.exe`、`windowsterminal.exe` 加进 `blocked_apps`（记得保留默认项）。
3. **点击授权框会收起开始菜单这类浮层**（Windows 对任何点在它外面的点击都会收起，与窗口是否被激活无关）。0.3.1 起授权后会重新核对窗口，变了就中止，0.3.2 起授权框显示在屏幕顶部居中、不会被遮住也不抢焦点（它运行在独立的子进程里，出任何问题都不会影响 Agent）。所以第一次批准一个应用后，Agent 可能报“授权期间目标窗口变了”：此时没有发送任何输入，让 Claude 重新 `screenshot` 并重试即可（应用已授权，不会再弹框）。
4. **提示词注入。** 屏幕上的网页、邮件、文档里的文字可能试图指挥模型。Skill 要求把它们当数据，但这不能完全杜绝。不要在授权了强力应用（终端、已登录的邮箱/银行）的同时，让模型去浏览不可信内容。
5. **隐私。** 每张截图都包含整个屏幕，并会发送给模型服务。屏幕上的私密信息会离开你的电脑。
6. **审计日志不是零敏感的。** 它只记 `type_text` / `write_file` / `edit_file` 内容的**长度和前 20 个字符**、文件路径和进程名；若开启 `save_screenshots` 则会落盘截图。日志目录和备份目录本身已被文件工具拦截。
7. **令牌是明文的。** 在 Windows 的 `config.json`（权限已收紧）和服务器的 `settings.json` 里。同一 Windows 账户下的其他程序读得到。令牌泄露 = 完全控制，请立即[轮换](#十升级轮换令牌与卸载)。
8. **黑名单只是尽力而为。** 内置敏感路径无法穷尽所有软件的凭据存放位置。
9. **无法操作 UAC 安全桌面**（有意为之）；默认无法操作管理员窗口（UIPI）；锁屏后不可用。
10. **Windows 后端没有被 CI 覆盖。** CI 用 `FakeBackend` 在 Linux 上跑全部自动化测试，真实的 SendInput / 窗口 / 弹窗代码只能靠你在 Windows 上做[手动验收](#windows-手动验收清单)。
11. **速度与成本。** 每步需要截图上传 + 模型推理 + 隧道往返，单步通常数秒；每张 1280 宽截图约 1000–1500 输入 token。

### 6.3 推荐做法清单

- [ ] 在专用 Windows 账户或虚拟机里运行；
- [ ] 保持 `approval_mode: prompt`，需要无人值守时再临时用 bypass，用完 `win-remote-off`；
- [ ] 隧道用专用密钥 + 无特权用户 + 受限 `authorized_keys`（4.2）；
- [ ] 令牌只放用户级文件，`chmod 600`，不进 Git、不进截图、不进 Issue；
- [ ] 不批准 Explorer / 终端类应用，或明确把终端加入 `blocked_apps`；
- [ ] 定期查看 `logs/actions.jsonl`；
- [ ] 有任何怀疑：甩鼠标到左上角 → `win-remote-off` → 轮换令牌。

发现漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

---

## 七、工具一览与使用

在 Claude Code 里直接说：

> 打开记事本，写一段 3 行的会议纪要，保存到桌面 test.txt。

| 工具 | 说明 |
|---|---|
| `screenshot(monitor?)` | 截图（JPEG）+ 尺寸/缩放比/显示器/前台窗口 |
| `zoom(x1,y1,x2,y2)` | 按截图坐标裁出区域，原生分辨率返回，**不改变**点击坐标系 |
| `click(x,y,button,clicks,modifiers)` | 单击/双击/右键/修饰键点击 |
| `move_mouse` · `drag` · `scroll` | 悬停 / 拖拽 / 滚轮 |
| `type_text(text,press_enter,method,expect_app,expect_handle)` | Unicode 安全输入（中文/日文/emoji）。`method="keys"`（默认）逐字符发送，不碰剪贴板；`method="paste"` 经剪贴板一次粘贴（备份并恢复文本剪贴板），长文本/多行更可靠，但密码框可能不接受 |
| `press_key(keys,repeat,expect_app,expect_handle)` | 如 `enter`、`ctrl+s`、`alt+tab`、`win+r`，兼容 xdotool 风格别名 |
| `wait(seconds≤30)` | 等待后截图 |
| `list_windows` · `focus_window(handle)` | 窗口管理 |
| `get_status` | 是否暂停/疑似锁屏、授权模式、已授权应用、显示器、缩放配置、文件工具范围、版本 |
| `read_file(path,offset,max_chars)` | 读文本文件，自动识别 UTF-8/UTF-16/GBK，返回编码与换行风格 |
| `list_files(path,pattern,recursive,max_entries)` | 列目录，可通配符过滤、递归（最多 4 层） |
| `write_file(path,content,mode,create_dirs)` | 写文本文件。`create`（默认，已存在则报错）/ `overwrite`（先备份）/ `append` |
| `edit_file(path,old_string,new_string,replace_all)` | 精确替换：`old_string` 必须逐字匹配且唯一，保留原编码和换行，改前备份 |
| `stat_file(path,sha256)` | 大小 / 修改时间 / 可选 sha256；报告未完成上传的续传位置 |
| `get_file(path,offset,length)` | **分块读取任意文件（含二进制）**，返回 base64，单块 ≤ 2 MiB |
| `put_file(path,data_b64,offset,final,overwrite,…)` | **分块上传任意文件（含二进制）**：临时文件 + 偏移校验 + sha256 校验 + 原子替换 |

所有输入类工具默认 `screenshot_after=true`：动作后等 `settle_ms` 再截图一并返回。坐标一律是**最近一次 `screenshot` 图片里的像素坐标**，Agent 负责换算成物理像素（含 DPI 缩放、多显示器偏移）。

能直接读写文件就不要靠截图点击：用 `read_file` / `edit_file` 比“打开记事本再点”快得多，也更稳。

### 文件传输：`winpc_cp.py`

`read_file` / `write_file` 只处理文本，而且内容要经过模型上下文——大文件会撑爆上下文，二进制文件根本传不了，文本里的 CRLF 还会被规范成 LF（实测：一个 517 字节的 CRLF 文件经 `write_file` 到 Windows 后变成 495 字节）。要在 Linux 与 Windows 之间**原样**搬运文件，用插件自带的脚本 [`claude-plugin/winpc/scripts/winpc_cp.py`](claude-plugin/winpc/scripts/winpc_cp.py)：它直接经隧道调用 Agent 的 `stat_file` / `get_file` / `put_file`，**文件内容不进入对话**，只用 Python 标准库（Linux 上不需要装任何包），令牌从 `WINPC_TOKEN` 环境变量读取（故意不接受命令行参数，免得出现在 `ps` 里）。

```bash
CP=~/winpc-remote/claude-plugin/winpc/scripts/winpc_cp.py     # 装了插件的话在 ~/.claude/plugins/ 下，可 find ~/.claude/plugins -name winpc_cp.py
python3 $CP put  ./report.pdf   'C:\Users\YOU\Desktop\'            # 上传；目标以 \ 或 / 结尾 = 目录，沿用文件名
python3 $CP get  'C:\Users\YOU\Desktop\data.zip'  ./downloads/    # 下载
python3 $CP stat 'C:\Users\YOU\Desktop\data.zip' --sha256         # 查看大小 / 校验和
python3 $CP put --overwrite --create-dirs ./a.bin 'C:\Users\YOU\Desktop\new\a.bin'
python3 $CP put --resume ./big.iso 'C:\Users\YOU\Desktop\'         # 传到一半断了：从断点继续
```

也可以直接对 Claude 说：“把 ./build/app.zip 传到我桌面”“把桌面上的 data.zip 拉到 ./downloads”，Skill 会引导它调用这个脚本。

**机制与保证**

- 分块（默认 1 MiB，最大 2 MiB）；上传先写与目标同目录的 `.<name>.winpc-part`，**最后一块校验 `expected_size` 与 `expected_sha256` 通过后才原子替换目标**。中途断线 / 被杀不会留下半截目标文件，也不会破坏已有同名文件。
- 下载先写 `<目标>.winpc-part`，sha256 与 Windows 端 `stat_file` 给出的值一致才改名；失败会清理临时文件。
- 每块的 `offset` 必须等于已接收大小，所以不会乱序或重复写入；`--resume` 用 `stat_file` 报告的 `partial_upload_size` 续传。
- 目标已存在默认拒绝；`--overwrite` 时 Windows 端先把原文件备份到 `logs\file_backups\`。
- 单文件上限 `transfer_max_bytes`（默认 512 MiB）。实测（本机回环）：100 MiB 约 4 秒下载；经 SSH 隧道的速度取决于你的网络。

**安全：传输工具与文件工具共用同一套闸门，没有旁路。** 写入需要你在 Windows 上授权（`prompt` 模式），`allowlist` 模式一律拒绝；敏感路径、Agent 自身目录、`--config` 指定的配置文件、可执行扩展名黑名单对上传都生效；**下载同样按文件内容拒绝私钥**（二进制也检查头部），`.env` / `.netrc` / 浏览器数据等同样拦截；紧急停止后上传被拒绝；审计日志只记路径和长度，**不记二进制内容**。因此 `.exe` / `.bat` / `.ps1` 默认传不上去——需要的话在 `config.json` 里调整 `file_blocked_write_ext`（会削弱防护，请想清楚）。

---

## 八、测试

### 自动化测试（Linux / Windows 均可，无需真实桌面）

```bash
git clone https://github.com/Tumi977/winpc-remote.git && cd winpc-remote
python3 -m venv .venv && . .venv/bin/activate     # Ubuntu/Debian 若报 ensurepip 错误：sudo apt install python3-venv
pip install -r requirements-dev.txt
NO_PROXY=127.0.0.1,localhost pytest -q
```

- `tests/test_unit.py`：坐标换算、多显示器偏移、按键解析、授权策略（黑/白名单、弹窗同意/拒绝、allowlist）、暂停、审计、锁屏提示；
- `tests/test_fileops.py`：路径策略（UNC、备用数据流、符号链接、敏感路径、按内容识别私钥、可执行扩展名）、编码与换行保持、备份、授权模式、`--config` 文件拦截、审计脱敏；
- `tests/test_transfer.py`：分块上传下载、偏移校验与续传、sha256/大小校验失败不留半截文件、覆盖保护与备份、授权/暂停/allowlist、私钥与凭据无法被 `get_file` 绕过、审计不记二进制，以及 `winpc_cp.py` 经真实 HTTP 的 3 MiB 往返、续传、错误码；
- `tests/test_typing_and_focus.py`：按键事件分组与节奏、授权后重新核对窗口、`expect_app` / `expect_handle`、粘贴模式（用模拟的 Win32 剪贴板 API 验证“无论成败都恢复剪贴板”）、授权框回退；
- `tests/test_keepalive.py`：复现间歇性 `ECONNRESET`（对照组证明 uvicorn 默认配置会在空闲 5 秒后断连）；
- `tests/test_e2e.py`：随机端口启动真实 HTTP MCP 服务（`FakeBackend`），用 MCP 客户端调用全部工具；校验 401 / 421、截图结果格式等。

在带 HTTP 代理的机器上务必设 `NO_PROXY=127.0.0.1,localhost`，否则请求会被代理截走，出现 502/404。

### 没有 Windows 也能联调 Linux 端

```bash
WINPC_TOKEN=some-fake-token python windows-agent/winpc_agent.py --backend fake --port 8799
# 另一个终端：
WINPC_TOKEN=some-fake-token WINPC_URL=http://127.0.0.1:8799/mcp claude mcp list
```

假 Agent 返回一块纯色画布，用来确认插件安装、环境变量展开和鉴权都正常。

### 实机冒烟测试

`windows-agent/bin/live_test.py`（需要 Agent 和隧道在线）在**服务器上**运行，只操作它自己打开的记事本，不保存文件。**如果检测到已有记事本窗口会直接中止**（新版记事本会恢复上次的标签页，脚本以“不保存”关闭它可能丢掉你未保存的内容）：

```bash
WINPC_TOKEN=… python windows-agent/bin/live_test.py            # 默认 http://127.0.0.1:8765/mcp
```

### 实机文件传输测试

把 Windows 端更新到 0.3.0 后，在服务器上运行（只在你给的目录里创建 `winpc-xfer-*` 测试文件，没有删除工具，测完请自行删除）：

```bash
WINPC_TOKEN=… python windows-agent/bin/live_transfer_test.py 'C:\Users\YOU\Desktop\winpc-transfer-test\'
```

它会上传 3 MiB 含 NUL 的二进制和一个 CRLF 文本、逐字节下载比对、检查覆盖保护与备份、并确认 `.exe` 上传被拦截。

### Windows 手动验收清单

1. 125% 缩放的显示器上，让 Claude 点击“开始”按钮和记事本菜单，位置准确。
2. 在记事本里输入中英文混合加 emoji，内容正确，且未改动剪贴板。
3. 首次操作 Chrome 时本机弹出确认框；点“否”后 Claude 停止并说明；点“是”后同一会话不再询问。
4. 打开黑名单应用，Claude 的操作被拒绝。
5. 操作过程中把鼠标甩到左上角，Agent 进入暂停，后续操作全部被拒绝。
6. 断开网络再恢复，隧道自动重连，Claude Code 能继续调用。
7. 锁屏后调用 `screenshot`，返回“屏幕可能已锁定”提示。
8. 让 Claude 读取一个内容是私钥的文件（可用 `ssh-keygen` 生成一把测试私钥），应被拒绝，即使在 bypass 模式下。
9. 端到端任务：“打开记事本，写一段 3 行的会议纪要，保存到桌面 test.txt”。

---

## 九、故障排查

| 现象 | 原因与处理 |
|---|---|
| **401 Unauthorized** | 令牌不一致。对比服务器 `settings.json` 里的 `WINPC_TOKEN` 和 Windows `config.json` 里的 `token`；改过环境变量后要重启 Claude Code |
| **421 Misdirected Request / Invalid Host** | Host 头不在允许列表。走 Tailscale 或自定义域名时，把 `IP:*` / `域名:*` 加进 `allowed_hosts` |
| 连接被拒绝 / 超时 | Agent 没启动，或隧道断了。Windows 上检查 Agent 与隧道窗口的输出；服务器上 `curl -i http://127.0.0.1:8765/mcp` 应返回 401（说明隧道通） |
| 隧道报 `remote port forwarding failed` | ① 服务器上该端口被占用（上次连接未释放）：换 `-RemotePort`，或稍等后重试；② 受限密钥的 `permitlisten` 与 `-RemotePort` 不一致 |
| `Permission denied (publickey)` | 公钥没放进隧道用户的 `authorized_keys`，或文件权限/属主不对（目录 700、文件 600、属主为该用户） |
| **点击位置偏移** | 多半是 DPI 感知失败。Agent 启动时调用 `SetProcessDpiAwareness(2)`；确认没有被其他启动器以兼容模式运行；点击前先重新 `screenshot`，坐标必须来自最近一张截图（不要用 `zoom` 图的坐标） |
| **`type_text` 在新版记事本等 WinUI 应用里丢字/错字**（一串字符变成同一个字符重复，如 `OK abc`→`OOOK ccc`） | 这类应用在处理按键（尤其回车附近）时会卡顿，期间堆积的 Unicode 按键被合并，**加大延迟也无法根治**（0.3.2 实测）。改用 `type_text(method="paste")`：经剪贴板一次 `Ctrl+V`，同一段 237 字符（含 emoji、全部 ASCII 符号、中文全角标点）逐字正确，且恢复了原剪贴板文本。中文输入法开着也会加重此问题，可先切到英文模式 |
| **中文乱码 / 输入丢字** | `type_text` 走 Unicode `SendInput`，与输入法无关。若仍异常，检查目标程序是否以管理员运行（UIPI 会静默丢输入）。PowerShell 脚本里的中文乱码，请确认脚本是 **UTF-8 带 BOM** 保存的（Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 GBK 读） |
| 截图全黑 / “屏幕可能已锁定” | 屏幕已锁定或显示器休眠；解锁并关闭自动锁屏。RDP 断开后也会黑屏，应在本机控制台会话中运行 |
| 工具超时 | 隧道慢或截图大。调大 `MCP_TOOL_TIMEOUT`；降低 `max_long_edge` / `jpeg_quality` |
| 提示“目标窗口以管理员身份运行” | UIPI 限制。需要时才以管理员身份重启 Agent（风险更高）；UAC 弹窗任何方式都无法操作 |
| “已被用户暂停” | 触发了紧急停止。在 Windows 本机重启 Agent |
| 文件工具提示“敏感位置 / 私钥 / Agent 自身目录” | 这是有意的拦截，任何模式下都不会放行。不要试图绕过 |
| 弹窗没出现 / 被拒绝 | 弹窗是置顶系统模态框，默认 60 秒内未点击视为拒绝；Agent 必须运行在交互式桌面会话（不是服务、不是 SSH 会话） |
| `win-remote-on` 提示找不到 `win-remote.config.json` | 按 3.4 复制示例并填写 |

---

## 十、升级、轮换令牌与卸载

**升级**

```powershell
cd "$HOME\winpc-remote"; git pull          # 然后 win-remote-off 再 win-remote-on
```

```bash
claude plugin marketplace update winpc-remote && claude plugin update winpc@winpc-remote
```

用 `get_status` 里的 `agent_version` 确认 Windows 端版本。

**轮换令牌**（泄露、怀疑泄露、换设备时）

1. Windows：`win-remote-off`，编辑 `config.json` 把 `"token"` 改成 `""`，再启动，记下新令牌；
2. 服务器：更新 `~/.claude/settings.json` 里的 `WINPC_TOKEN`，重启 Claude Code；
3. 如果令牌曾出现在日志、截图、聊天或 Issue 里，把它们一并清理。

**吊销隧道密钥**：从服务器 `/home/winpc-tunnel/.ssh/authorized_keys` 删除对应那一行。

**卸载**

```bash
claude plugin uninstall winpc@winpc-remote && claude plugin marketplace remove winpc-remote
```

Windows 上 `win-remote-off`，删除计划任务 `schtasks /Delete /TN WinPCAgent /F`，删除项目目录。

---

## 十一、项目结构、变更与许可

```
windows-agent/   winpc_agent.py（MCP 服务器、授权、鉴权、审计）· backends.py（Windows / Fake 后端）
                 fileops.py（文件读写与路径策略）· start-agent.ps1 · start-tunnel.ps1 · win-remote.ps1 · bin/
claude-plugin/   Claude Code 插件 winpc（.mcp.json + Skill + scripts/winpc_cp.py 文件传输脚本）
.claude-plugin/  marketplace.json（让本仓库可被 `claude plugin marketplace add` 直接安装）
tests/           单元测试 + 端到端测试（FakeBackend，Linux 上即可运行）
```

### 变更记录

完整变更见 [CHANGELOG.md](CHANGELOG.md)。最近的几项：

- **0.3.6** 修复授权框超时会让 Agent 进程崩溃（改为子进程，进程隔离）；
- **0.3.5** 新增 `expect_handle`：按具体窗口句柄校验，分得开多个终端窗口；
- **0.3.4** 新增 `expect_app`：前台窗口不是预期应用就拒绝发送按键（实测中按键被发进 VS Code 三次）；
- **0.3.3** 修复间歇性 `ECONNRESET`（uvicorn 默认只保持连接 5 秒）；传输脚本退出时关闭 MCP 会话；
- **0.3.2** 授权框改到屏幕顶部居中、不再被开始菜单遮住；新增 `type_text(method="paste")`；
- **0.3.0** 文件传输（二进制、分块、校验、断点续传）；**0.2.1** 安全加固（按内容识别私钥等）。

**升级**：覆盖 `windows-agent/` 后重启 Agent，`config.json` 不用改（新增配置项都有默认值）；Linux 端 `claude plugin update winpc@winpc-remote`。

### 贡献

欢迎 Issue 和 PR。提交前请运行 `pytest -q`。涉及授权、路径策略的改动，请同时补充回归测试，并在 PR 里说明威胁模型。**不要**在 Issue、PR 或日志里贴令牌、私钥、截图中的私人信息。

### 许可

[MIT](LICENSE)。本软件按“原样”提供，不提供任何担保；使用它所造成的一切后果由使用者自行承担。

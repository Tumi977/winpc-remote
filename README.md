# WinPC Remote

Let **Claude Code running on a remote Linux server** operate **your local Windows PC** through continuous screenshots, keyboard/mouse input, file reads/writes and binary file transfer.

- **Windows side**: a resident agent (`winpc-agent`) that exposes screenshot, click, typing, key-press, file and transfer tools over MCP (Streamable HTTP).
- **Linux side**: a Claude Code plugin `winpc` (MCP config + Skill) that connects to the agent through an **SSH reverse tunnel** (or Tailscale).

Claude Desktop's built-in computer use has no external interface, so a remote Claude Code cannot borrow it. This project provides the missing piece: a Windows-side executor plus a secure channel.

Chinese version: [README.zh-CN.md](README.zh-CN.md)

> **This is a third-party community project and is not affiliated with Anthropic.**

> ## ⚠️ Read this first
> Installing this means **letting a model that can be influenced by what is on your screen operate your keyboard and mouse, as you, on your desktop.**
> The project provides several layers of protection (token, app authorization, sensitive-path blocking, emergency stop, audit log), but it is **not a sandbox**.
> Please read [Security model and known risks](#6-security-model-and-known-risks) in full, especially the sections on `bypass` mode and on why "app authorization is not a sandbox".
> **Strongly recommended:** run it in a dedicated Windows account or a virtual machine.

> **Language note:** the agent's tool results, error messages and the approval dialog are currently in Chinese (a few are quoted below with their meaning). The tool names, parameters and configuration are all in English.

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Quick start (5 steps)](#2-quick-start-5-steps)
3. [Windows setup](#3-windows-setup)
4. [Networking](#4-networking)
5. [Linux setup](#5-linux-setup)
6. [Security model and known risks](#6-security-model-and-known-risks)
7. [Tools and usage](#7-tools-and-usage)
8. [Testing](#8-testing)
9. [Troubleshooting](#9-troubleshooting)
10. [Upgrading, rotating the token, uninstalling](#10-upgrading-rotating-the-token-uninstalling)
11. [Project layout, changes and license](#11-project-layout-changes-and-license)

---

## 1. How it works

```
┌──────────────── Linux server ────────────────┐          ┌──── Local Windows (logged in, screen unlocked) ────┐
│  Claude Code                                 │          │  winpc-agent (Python, in your desktop session)     │
│   ├─ plugin winpc                            │          │   ├─ MCP server (Streamable HTTP) 127.0.0.1:8765   │
│   │   ├─ .mcp.json → http://127.0.0.1:8765   │   SSH    │   ├─ Bearer-token check + Host/Origin check        │
│   │   └─ skills/windows-pc-control/SKILL.md  │◄═reverse═│   ├─ App authorization (prompt/allowlist/blocklist)│
│   └─ model: reads screenshots, decides       │  tunnel  │   ├─ Screenshots: mss → resize → JPEG              │
│  127.0.0.1:8765 ← tunnel exit (server only)  │   (-R)   │   ├─ Input: SendInput (Unicode-safe)               │
└──────────────────────────────────────────────┘          │   ├─ Emergency stop: mouse to top-left → pause     │
                                                          │   └─ Audit log: logs/actions.jsonl                 │
                                                          └────────────────────────────────────────────────────┘
```

The connection is **initiated by Windows**: `ssh -R` on Windows maps the agent port to `127.0.0.1` on the server, so Windows works behind NAT and the agent itself is never exposed to any network.

### Prerequisites

| | Requirement |
|---|---|
| Windows | Windows 10/11 with a **logged-in interactive desktop session** (not a service, not an SSH session; unusable while the screen is locked), Python 3.10+, OpenSSH client (install via *Settings → Optional features*) |
| Linux | A server that can run Claude Code, with an OpenSSH server (`sshd`; OpenSSH ≥ 7.8 recommended because the restricted-key setup below needs it) |
| Other | A network path that lets Windows reach the server over SSH (a public IP or a jump host both work) |

---

## 2. Quick start (5 steps)

```text
1. Windows:      clone the repo, run windows-agent\start-agent.ps1  → note the token printed on first start
2. Server:       create an unprivileged tunnel-only user and give it a restricted SSH public key     (chapter 4)
3. Windows:      fill in win-remote.config.json, run win-remote-on                                   (chapters 3 and 4)
4. Server:       put the token into ~/.claude/settings.json (env), install the plugin                (chapter 5)
5. Claude Code:  /mcp shows winpc as connected → "use winpc's get_status to check my PC"
```

No Windows machine and just want to try the Linux side? See `--backend fake` in [Testing](#8-testing).

---

## 3. Windows setup

### 3.1 Get the code and start the agent

```powershell
git clone https://github.com/Tumi977/winpc-remote.git "$HOME\winpc-remote"
cd "$HOME\winpc-remote\windows-agent"
powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
```

> Put the project **inside your user profile** (as above), not in a drive root such as `D:\` or `E:\`: the default permissions there may let other local accounts read `config.json`. `start-agent.ps1` also tightens that file's permissions (see 3.2).

`start-agent.ps1` does, in order: detect Python 3.10+ → create `.venv` → install dependencies → create `config.json` from `config.example.json` → tighten `config.json` permissions → start the agent.

**The first start generates an access token and prints it only once** (it is also stored in plain text in `config.json`). Copy it; you need it in chapter 5.

> The token is the key to full control of your PC. Do not share it, commit it to Git or paste it into an issue.
> You can also set the token with the `WINPC_TOKEN` environment variable, which takes precedence over the config file.

### 3.2 Configuration (`config.json`)

`config.json` is created from `config.example.json` and is ignored by `.gitignore`. The agent can also read a config file from elsewhere via `--config`.

| Key | Default | Description |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8765` | Listens on loopback only. With Tailscale, use the Tailscale interface IP. **Do not** bind `0.0.0.0` or a LAN address |
| `token` | generated | Bearer token |
| `allowed_hosts` | `[]` | Extra allowed `Host` headers (`127.0.0.1:*` and `localhost:*` are built in; a non-loopback bind IP is allowed automatically) |
| `approval_mode` | `prompt` | `prompt`: the first operation on an unapproved app/file shows an always-on-top confirmation box on the PC; `allowlist`: anything not allow-listed is refused; `all`: everything is allowed (**not recommended**, see 6.2) |
| `approval_timeout_s` | `60` | Dialog timeout; a timeout counts as "deny" |
| `allowed_apps` | `["notepad.exe"]` | Allow-list (process names, case-insensitive, `.exe` optional) |
| `blocked_apps` | password managers etc. | Block-list; highest priority, refused in every mode. **Setting it replaces the default list**, so include the defaults you still want |
| `max_long_edge` | `1280` | Long edge of screenshots in pixels. Roughly 1,000–1,500 tokens per image; can be raised to 1568 |
| `jpeg_quality` / `settle_ms` | `70` / `600` | JPEG quality / how long to wait after an action before taking the screenshot |
| `log_dir` / `save_screenshots` | `logs` / `false` | Audit log directory (relative to the config file) / whether to save every screenshot (screenshots contain everything on screen; off by default) |
| `file_tools_enabled` | `true` | Set to `false` to not register the file tools |
| `file_roots` | Desktop, Documents (incl. OneDrive redirects) | **Reads** inside these need no confirmation; **writes always need authorization**. `~` is supported |
| `file_blocked_paths` | `[]` | Extra path substrings (lower-case, forward slashes) blocked on top of the built-in sensitive paths |
| `type_chunk_chars` / `type_delay_ms` | `1` / `5` | Pacing of `type_text`: characters per `SendInput` call and the delay between batches in ms. Default is one character at a time plus 5 ms (about 200 chars/s). Some apps (e.g. the new Notepad) cannot keep up with fast input and drop characters; raise `type_delay_ms` if that happens |
| `type_newline_delay_ms` | `150` | Extra wait after Enter. Enter makes some apps (e.g. the new Notepad) stall, and keystrokes queued meanwhile get merged into one repeated character |
| `paste_restore_delay_ms` | `600` | How long to wait after `type_text(method="paste")` before restoring the original clipboard (the app must have read it first) |
| `transfer_max_bytes` | `536870912` (512 MiB) | Per-file transfer limit (`get_file` / `put_file`) |
| `file_blocked_write_ext` | `.exe .bat .ps1 .lnk .reg .jse .url …` | Extensions that may not be written (auto-executable content). **Setting it replaces the default list** |

**Permissions of `config.json`**: the token is stored in plain text, so `start-agent.ps1` runs `icacls` on every start to disable inheritance and grant read/write to the current user only. If you do not use that script, do it yourself:

```powershell
icacls .\config.json /inheritance:r /grant:r "$([Security.Principal.WindowsIdentity]::GetCurrent().Name):(R,W)"
```

**Built-in sensitive paths and files** (read **and** write refused, **in every approval mode, including `all`/bypass**):

- Directories: `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker`, `.config/gh`, `.config/gcloud`, `.claude`, Windows credential and DPAPI directories, Chrome / Edge / Firefox / Brave / Opera / Vivaldi user data, PowerShell command history, the Startup folder;
- File names: `.env`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `credentials.json`, `ntuser.dat`, `id_rsa*` / `id_ed25519*` and similar, `*.kdbx` `*.pfx` `*.p12` `*.ppk`;
- **Files whose content is a private key** (containing `-----BEGIN … PRIVATE KEY-----` or a PuTTY key header): refused for reading and modification regardless of location or name;
- **The agent's own directory, the log directory, and the config file you pass with `--config`** (it contains the token).

This is a best-effort **block-list**, not a guarantee: it cannot catch a secret stored anywhere in an unusual format. Keep truly important credentials in a password manager.

### 3.3 Start on login

The agent **cannot run as a Windows service** (services run in session 0 and cannot see your desktop). Use a scheduled task that runs at logon:

```powershell
schtasks /Create /TN WinPCAgent /SC ONLOGON /RL LIMITED /TR "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Users\YOU\winpc-remote\windows-agent\start-agent.ps1"
```

You cannot see the token that is generated on first start when running in the background, so **start it manually once first** and note the token.

### 3.4 One-command start/stop (agent + tunnel)

`windows-agent\bin\` contains three commands (add that directory to `PATH` to use them from anywhere):

| Command | What it does |
|---|---|
| `win-remote-on` | Starts the agent and the tunnel (one window each; keep them open); unapproved apps show a confirmation box |
| `win-remote-bypass` | Same, but the agent starts with `--bypass-permissions`: **app and file authorization prompts are skipped**; the block-list and sensitive-path blocking still apply. Use with care, see 6.2 |
| `win-remote-off` | Stops the agent and the tunnel |

They read `windows-agent\win-remote.config.json` (ignored by `.gitignore`). First use:

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

## 4. Networking

### Option A (recommended): SSH reverse tunnel

The agent listens only on `127.0.0.1` of the Windows machine and is not exposed to any network; the tunnel is encrypted end to end; Windows works behind NAT.

#### 4.1 Generate a **dedicated** key on Windows

Do not reuse the key you use to log in to the server. Keep it in `%USERPROFILE%\.ssh\` (that directory is blocked for the agent's file tools):

```powershell
ssh-keygen -t ed25519 -f "$HOME\.ssh\winpc_tunnel_ed25519" -C "winpc-tunnel"
# public key: $HOME\.ssh\winpc_tunnel_ed25519.pub  (copy only the .pub file to the server)
```

> If you keep a private key outside `.ssh` (for example `C:\Users\me\ssh\xxx`), the agent still refuses to read it because its content is a private key; but keeping it in `.ssh` remains the safer habit.

#### 4.2 Create a tunnel-only user on the server and restrict this key

The tunnel only needs to "listen on `127.0.0.1:8765` on the server". It needs no shell, let alone root. Use an unprivileged user and restrict the public key so it can do only that:

```bash
sudo useradd -m -s /usr/sbin/nologin winpc-tunnel
sudo install -d -m 700 -o winpc-tunnel -g winpc-tunnel /home/winpc-tunnel/.ssh
```

Write the **single line** below into `/home/winpc-tunnel/.ssh/authorized_keys` (replace the end with the contents of your `.pub` file), mode 600, owned by `winpc-tunnel`:

```text
restrict,port-forwarding,permitlisten="127.0.0.1:8765",permitopen="127.0.0.1:1",command="/bin/echo tunnel-only" ssh-ed25519 AAAA…YOUR-PUBLIC-KEY… winpc-tunnel
```

What each option does (each one tested on OpenSSH 8.9):

| Option | Effect |
|---|---|
| `restrict` | Disables pty, agent/X11 forwarding, user rc and every other default capability |
| `port-forwarding` | Re-enables port forwarding (the tunnel needs it) |
| `permitlisten="127.0.0.1:8765"` | Allows `-R` to listen on that one address:port only; requests for other remote ports are refused |
| `permitopen="127.0.0.1:1"` | Restricts `-L` local forwarding to a useless target, which effectively disables it |
| `command="…"` | Forced command. `restrict` does **not** stop command execution, so this is required: even if the key leaks, an attacker only gets the single line `tunnel-only` |

Observed results: `-R 127.0.0.1:8765` is accepted and forwards traffic; `-R` to any other port is refused (`remote port forwarding failed`); `ssh host 'id'` prints only `tunnel-only`; `-L` is refused.

Make sure forwarding is not disabled in the server's `sshd_config` (it is allowed by default):

```text
AllowTcpForwarding yes      # must not be "no"
GatewayPorts no             # the default; keep "no" so the forwarded port binds to loopback only
```

> **Other users on the same server** can also reach `127.0.0.1:8765`, which is why the token is mandatory, not optional.

#### 4.3 Start the tunnel on Windows

Using `win-remote-on` from 3.4 is recommended. You can also run it on its own:

```powershell
.\start-tunnel.ps1 -Server your.server.example.com -User winpc-tunnel -KeyFile $HOME\.ssh\winpc_tunnel_ed25519
# optional: -Port 8765 -RemotePort 8765 -SshPort 22
```

The script uses `ServerAliveInterval=30`, `ExitOnForwardFailure=yes` and `IdentitiesOnly=yes`, reconnects 5 seconds after a drop, and uses `StrictHostKeyChecking=accept-new` on first connect (trust on first use, then verify the host key).

Verify on the server that the tunnel works (**expect 401**, which means the tunnel is up, the agent is running and token checking is active):

```bash
curl -i http://127.0.0.1:8765/mcp
```

### Option B: Tailscale / ZeroTier

1. Join both Windows and Linux to the same tailnet.
2. Set `"host": "100.x.y.z"` in `config.json` (the Windows Tailscale IP).
3. On Linux, `export WINPC_URL=http://100.x.y.z:8765/mcp` (or put it in the `env` block of `settings.json`).

In this setup the agent listens on the virtual interface and traffic is encrypted by WireGuard, but **keep the token** and use Tailscale ACLs to limit which devices can reach it. The agent itself speaks plain HTTP: **never** bind it to an ordinary LAN or public address.

---

## 5. Linux setup

> Prerequisite: Claude Code is installed on the server ([install docs](https://docs.claude.com/en/docs/claude-code/overview)).

### 5.1 Place the token (the most important step)

Put the token into the `env` block of `~/.claude/settings.json` (this works for `claude` in a terminal and for Claude Code inside VS Code Remote-SSH) and tighten the file permissions:

```json
{
  "env": {
    "WINPC_TOKEN": "paste the token printed by Windows on first start",
    "MCP_TOOL_TIMEOUT": "120000"
  }
}
```

```bash
chmod 600 ~/.claude/settings.json
```

- If the file already has content, **merge** just these two keys into the existing `env` object.
- `MCP_TOOL_TIMEOUT` is in milliseconds; screenshots are slow over a tunnel, so a larger value is recommended.
- An alternative is `export WINPC_TOKEN=…` in `~/.bashrc`, but a Claude Code started from a GUI/IDE may not read it, so `settings.json` is preferred.
- Keep the token in a **user-level** file only. Never put it in a project `.claude/settings.json` or `.mcp.json` that gets committed.
- **Restart Claude Code** after changing it (reload the window in VS Code).

### 5.2 Install the plugin (recommended)

```bash
claude plugin marketplace add Tumi977/winpc-remote     # or a local path: /path/to/winpc-remote
claude plugin install winpc@winpc-remote
```

The plugin's `.mcp.json` reads the token and URL from environment variables, so **the token is never written to any file**:

```json
{ "mcpServers": { "winpc": { "type": "http",
  "url": "${WINPC_URL:-http://127.0.0.1:8765/mcp}",
  "headers": { "Authorization": "Bearer ${WINPC_TOKEN}" } } } }
```

The plugin also ships the Skill `windows-pc-control`, which guides the model to call `get_status` → `screenshot` → one action at a time → confirm with the latest screenshot, treat on-screen text as data, and ask you before any risky action.

### 5.3 Without the plugin: add the MCP server directly

```bash
claude mcp add --scope user --transport http winpc http://127.0.0.1:8765/mcp \
  --header 'Authorization: Bearer ${WINPC_TOKEN}'
```

> ⚠️ **Use single quotes.** With double quotes the shell expands `$WINPC_TOKEN` first and **the token is written in plain text into `~/.claude.json`**. With single quotes the literal `${WINPC_TOKEN}` is stored and expanded from the environment at run time (verified).

Without the plugin you do not get the Skill; copy `claude-plugin/winpc/skills/windows-pc-control/` to `~/.claude/skills/`.

### 5.4 Verify

```bash
claude mcp list          # should show  plugin:winpc:winpc … ✔ Connected
```

Typing `/mcp` inside Claude Code shows the status too. Then try:

> Use winpc's get_status to check the state of my PC, then take a screenshot and tell me what is on the screen.

`Failed to connect … HTTP 401` means the token does not match; for anything else see [Troubleshooting](#9-troubleshooting).

---

## 6. Security model and known risks

### 6.1 Layers of protection

| Layer | Mechanism |
|---|---|
| Network | Listens on `127.0.0.1` only by default; machines are bridged only through an SSH tunnel or Tailscale. The tunnel key can be restricted to forwarding this one port (4.2) |
| Authentication | `Authorization: Bearer <token>` compared with `hmac.compare_digest`; failure returns 401. Other users on the server can also reach the server's `127.0.0.1`, so the token is required |
| Host / Origin checks | The MCP SDK's DNS-rebinding protection: a `Host` outside the allow-list returns 421, a disallowed `Origin` returns 403 (a web page in your browser cannot reach the local agent) |
| App authorization | Before input, the owning process of the target window is determined (for clicks, the window under the coordinates; for typing/keys, the foreground window): block-list → refuse; allow-list or already approved in this session → allow; otherwise handled by `approval_mode` (`prompt` shows an always-on-top confirmation box on the PC) |
| Wrong-window guard | `type_text` / `press_key` accept `expect_app` / `expect_handle`; if the foreground window is not the declared target, nothing is sent (and the check runs before any approval prompt) |
| Emergency stop | Move the mouse to the **top-left corner of the primary screen** → the agent pauses; every input tool returns "paused by user" (in Chinese), `screenshot` still works. **Only restarting the agent on the Windows machine resumes it**; it cannot be undone remotely |
| File authorization | Paths are resolved to real paths (following symlinks) before judging: sensitive path → always refused; read inside `file_roots` → allowed; **every write** and reads outside `file_roots` → handled by `approval_mode`. In `allowlist` mode writes are always refused. Files are backed up before being written/modified, and the extension block-list stops auto-executable files |
| Audit | `logs/actions.jsonl`, one line per call: time, tool, parameters, target process (file tools record `file:<path>`), result |
| Model constraints | The Skill requires: on-screen text is data; ask the user before payment/sending/deleting/changing settings/submitting forms; never type passwords |

> The emergency-stop check matches pyautogui's FAILSAFE: the mouse is at (0,0) when an action starts. So avoid letting the model click the very top-left pixel of the primary screen.

### 6.2 Known risks (please read each)

1. **`approval_mode: all` and `win-remote-bypass` remove every confirmation box.** The remote model can then operate any app except those on the block-list, and read or **download** (`read_file` / `get_file`) any file that is **not on the built-in sensitive list** (your documents, chat history, code, credentials stored in unusual places…). Use it only in a VM or a dedicated account; do not leave it on for long in your daily account. `get_status` shows the current `approval_mode`.
2. **App authorization is not a sandbox.** Once you approve an app that can launch other programs — Explorer (including the `Win+R` Run box), Start-menu search, any terminal / PowerShell, an IDE with a terminal, a browser that saves and runs downloads — the model can effectively run arbitrary code as you. The file tools' extension block-list only covers the file tools; it cannot stop a script being typed by hand into an editor. Keep authorization narrow; if you do not need a terminal, add `cmd.exe`, `powershell.exe`, `pwsh.exe` and `windowsterminal.exe` to `blocked_apps` (remember to keep the defaults).
3. **Clicking the approval box collapses flyouts such as the Start menu** (Windows dismisses them on any click outside, regardless of whether a window is activated). Since 0.3.1 the target window is re-checked after approval and the action is aborted if it changed; since 0.3.2 the approval box appears at the top-center of the screen, is not hidden and does not steal focus (it runs in a separate subprocess, so nothing it does can affect the agent). So after you approve an app for the first time the agent may report "the target window changed during authorization" (in Chinese): nothing was sent; just let Claude take a new `screenshot` and retry (the app is already approved, so no second prompt).
4. **Prompt injection.** Text in a web page, email or document on screen may try to steer the model. The Skill tells it to treat that as data, but this cannot be fully prevented. Do not let the model browse untrusted content while powerful apps (a terminal, a logged-in mailbox or bank) are authorized.
5. **Privacy.** Every screenshot contains the whole screen and is sent to the model service. Private information on screen leaves your computer.
6. **The audit log is not free of sensitive data.** It records only the **length and first 20 characters** of `type_text` / `write_file` / `edit_file` content, file paths and process names; with `save_screenshots` enabled screenshots are stored on disk. The log and backup directories are themselves blocked for the file tools.
7. **The token is in plain text.** In `config.json` on Windows (permissions tightened) and in `settings.json` on the server. Other programs under the same Windows account can read it. A leaked token means full control; [rotate it](#10-upgrading-rotating-the-token-uninstalling) immediately.
8. **The block-list is best effort.** The built-in sensitive paths cannot cover where every piece of software keeps its credentials.
9. **The UAC secure desktop cannot be operated** (by design); elevated windows cannot be operated by default (UIPI); unusable while the screen is locked.
10. **The Windows backend is not covered by CI.** CI runs all automated tests on Linux with `FakeBackend`; the real SendInput / window / dialog code can only be verified by you on Windows with the [manual acceptance checklist](#windows-manual-acceptance-checklist).
11. **Speed and cost.** Each step needs a screenshot upload + model inference + a tunnel round trip, usually several seconds; each 1280-px-wide screenshot costs roughly 1,000–1,500 input tokens.

### 6.3 Recommended practices

- [ ] Run in a dedicated Windows account or a virtual machine;
- [ ] Keep `approval_mode: prompt`; use bypass only temporarily when you need unattended operation, then run `win-remote-off`;
- [ ] Use a dedicated tunnel key + an unprivileged user + a restricted `authorized_keys` line (4.2);
- [ ] Keep the token only in user-level files, `chmod 600`, out of Git, screenshots and issues;
- [ ] Do not approve Explorer / terminal-type apps, or explicitly add terminals to `blocked_apps`;
- [ ] Review `logs/actions.jsonl` regularly;
- [ ] If anything looks wrong: move the mouse to the top-left corner → `win-remote-off` → rotate the token.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

---

## 7. Tools and usage

Just tell Claude Code, for example:

> Open Notepad, write a 3-line meeting note, and save it to the desktop as test.txt.

| Tool | Description |
|---|---|
| `screenshot(monitor?)` | Screenshot (JPEG) plus size / scale / monitors / foreground window |
| `zoom(x1,y1,x2,y2)` | Crops a region by screenshot coordinates and returns it at native resolution; **does not change** the click coordinate system |
| `click(x,y,button,clicks,modifiers)` | Click / double-click / right-click / modifier-click |
| `move_mouse` · `drag` · `scroll` | Hover / drag / mouse wheel |
| `type_text(text,press_enter,method,expect_app,expect_handle)` | Unicode-safe typing (Chinese/Japanese/emoji). `method="keys"` (default) sends one character at a time without touching the clipboard; `method="paste"` pastes once through the clipboard (the text clipboard is backed up and restored), which is more reliable for long or multi-line text but may be refused by password fields |
| `press_key(keys,repeat,expect_app,expect_handle)` | e.g. `enter`, `ctrl+s`, `alt+tab`, `win+r`; xdotool-style aliases are accepted |
| `wait(seconds≤30)` | Wait, then take a screenshot |
| `list_windows` · `focus_window(handle)` | Window management |
| `get_status` | Paused / probably locked, approval mode, approved apps, monitors, scaling, file-tool scope, version |
| `read_file(path,offset,max_chars)` | Reads a text file; detects UTF-8/UTF-16/GBK and reports encoding and line-ending style |
| `list_files(path,pattern,recursive,max_entries)` | Lists a directory; glob filter, recursion (up to 4 levels) |
| `write_file(path,content,mode,create_dirs)` | Writes a text file. `create` (default, errors if it exists) / `overwrite` (backs up first) / `append` |
| `edit_file(path,old_string,new_string,replace_all)` | Exact replacement: `old_string` must match verbatim and be unique; keeps the original encoding and line endings; backs up first |
| `stat_file(path,sha256)` | Size / mtime / optional sha256; reports the resume offset of an unfinished upload |
| `get_file(path,offset,length)` | **Chunked read of any file (binary included)**, returned as base64, up to 2 MiB per chunk |
| `put_file(path,data_b64,offset,final,overwrite,…)` | **Chunked upload of any file (binary included)**: temp file + offset check + sha256 check + atomic replace |

All input tools default to `screenshot_after=true`: after the action they wait `settle_ms` and return a screenshot. Coordinates are always **pixel coordinates in the most recent `screenshot` image**; the agent converts them to physical pixels (including DPI scaling and multi-monitor offsets).

If you can read or write a file directly, do not click through the screen: `read_file` / `edit_file` are far faster and more reliable than "open Notepad and click around".

### File transfer: `winpc_cp.py`

`read_file` / `write_file` handle text only and their content passes through the model's context: large files would blow up the context, binary files cannot be sent at all, and CRLF in text gets normalized to LF (measured: a 517-byte CRLF file became 495 bytes after `write_file` to Windows). To move files between Linux and Windows **byte for byte**, use the script that ships with the plugin, [`claude-plugin/winpc/scripts/winpc_cp.py`](claude-plugin/winpc/scripts/winpc_cp.py). It calls the agent's `stat_file` / `get_file` / `put_file` directly through the tunnel, so **file content never enters the conversation**; it uses only the Python standard library (nothing to install on Linux) and reads the token from the `WINPC_TOKEN` environment variable (it deliberately does not accept it as a command-line argument, so it never shows up in `ps`).

```bash
CP=~/winpc-remote/claude-plugin/winpc/scripts/winpc_cp.py     # with the plugin installed it lives under ~/.claude/plugins/: find ~/.claude/plugins -name winpc_cp.py
python3 $CP put  ./report.pdf   'C:\Users\YOU\Desktop\'            # upload; a target ending in \ or / is a directory, the file name is kept
python3 $CP get  'C:\Users\YOU\Desktop\data.zip'  ./downloads/    # download
python3 $CP stat 'C:\Users\YOU\Desktop\data.zip' --sha256         # size / checksum
python3 $CP put --overwrite --create-dirs ./a.bin 'C:\Users\YOU\Desktop\new\a.bin'
python3 $CP put --resume ./big.iso 'C:\Users\YOU\Desktop\'         # interrupted halfway: continue from where it stopped
```

You can also just tell Claude: "send ./build/app.zip to my desktop" or "pull data.zip from my desktop into ./downloads"; the Skill guides it to use this script.

**Mechanism and guarantees**

- Chunked (1 MiB by default, 2 MiB max). An upload first writes `.<name>.winpc-part` next to the target, and **only after the last chunk passes the `expected_size` and `expected_sha256` checks is the target atomically replaced**. A dropped connection or a killed process never leaves a half-written target and never damages an existing file of the same name.
- A download writes `<target>.winpc-part` first and renames it only when its sha256 matches the value reported by `stat_file` on Windows; on failure the temp file is removed.
- Each chunk's `offset` must equal the size received so far, so chunks can neither arrive out of order nor be written twice; `--resume` continues from the `partial_upload_size` reported by `stat_file`.
- An existing target is refused by default; with `--overwrite` the original is first backed up to `logs\file_backups\` on Windows.
- Per-file limit `transfer_max_bytes` (512 MiB by default). Measured on loopback: 100 MiB downloads in about 4 seconds; over an SSH tunnel the speed depends on your network.

**Security: transfers go through exactly the same gates as the file tools — there is no side door.** Writing needs your authorization on Windows (`prompt` mode) and is always refused in `allowlist` mode; sensitive paths, the agent's own directory, the file given to `--config` and the executable-extension block-list all apply to uploads; **downloads also refuse private keys by content** (binary files are checked at the head too), and `.env` / `.netrc` / browser data and similar are blocked as well; uploads are refused after an emergency stop; the audit log records only paths and lengths, **never binary content**. That is why `.exe` / `.bat` / `.ps1` cannot be uploaded by default — if you need that, adjust `file_blocked_write_ext` in `config.json` (it weakens protection, so think it through).

---

## 8. Testing

### Automated tests (Linux or Windows, no real desktop needed)

```bash
git clone https://github.com/Tumi977/winpc-remote.git && cd winpc-remote
python3 -m venv .venv && . .venv/bin/activate     # on Ubuntu/Debian, if you get an ensurepip error: sudo apt install python3-venv
pip install -r requirements-dev.txt
NO_PROXY=127.0.0.1,localhost pytest -q
```

- `tests/test_unit.py`: coordinate conversion, multi-monitor offsets, key parsing, authorization policy (block/allow-list, dialog allow/deny, allowlist mode), pause, audit, lock-screen hint;
- `tests/test_fileops.py`: path policy (UNC, alternate data streams, symlinks, sensitive paths, private-key detection by content, executable extensions), encoding and line-ending preservation, backups, authorization modes, blocking of the `--config` file, audit redaction;
- `tests/test_transfer.py`: chunked upload/download, offset checks and resume, no half-written files when sha256/size checks fail, overwrite protection and backups, authorization/pause/allowlist, private keys and credentials cannot be bypassed via `get_file`, audit never records binary content, plus `winpc_cp.py` over real HTTP: a 3 MiB round trip, resume and error codes;
- `tests/test_typing_and_focus.py`: key-event grouping and pacing, re-checking the window after approval, `expect_app` / `expect_handle`, paste mode (a mocked Win32 clipboard API verifies that the clipboard is restored whether or not the paste succeeds), approval-dialog fallback;
- `tests/test_keepalive.py`: reproduces the intermittent `ECONNRESET` (a control group shows that uvicorn's default configuration drops a connection after 5 idle seconds);
- `tests/test_e2e.py`: starts a real HTTP MCP server on a random port (`FakeBackend`) and calls every tool with an MCP client; checks 401 / 421, the screenshot result format, etc.

On a machine behind an HTTP proxy always set `NO_PROXY=127.0.0.1,localhost`, otherwise requests are intercepted by the proxy and you see 502/404.

### Try the Linux side without Windows

```bash
WINPC_TOKEN=some-fake-token python windows-agent/winpc_agent.py --backend fake --port 8799
# in another terminal:
WINPC_TOKEN=some-fake-token WINPC_URL=http://127.0.0.1:8799/mcp claude mcp list
```

The fake agent returns a solid-colour canvas; it is used to confirm that plugin installation, environment-variable expansion and authentication all work.

### Live smoke test

`windows-agent/bin/live_test.py` (needs the agent and tunnel online) runs **on the server**; it only operates a Notepad it opens itself and does not save files. **It aborts if it finds an existing Notepad window** (the new Notepad restores your previous tabs, and the script closes it choosing "don't save", which could discard your unsaved content):

```bash
WINPC_TOKEN=… python windows-agent/bin/live_test.py            # default http://127.0.0.1:8765/mcp
```

### Live file-transfer test

Run on the server (it only creates `winpc-xfer-*` test files in the directory you give; there is no delete tool, so remove them yourself afterwards):

```bash
WINPC_TOKEN=… python windows-agent/bin/live_transfer_test.py 'C:\Users\YOU\Desktop\winpc-transfer-test\'
```

It uploads a 3 MiB binary containing NUL bytes and a CRLF text file, downloads them back and compares byte for byte, checks overwrite protection and backups, and confirms that a `.exe` upload is blocked.

### Windows manual acceptance checklist

1. On a display with 125% scaling, ask Claude to click the Start button and a Notepad menu; the positions are accurate.
2. Type mixed Chinese/English text plus emoji into Notepad; the content is correct and the clipboard is not changed.
3. On first use of Chrome a confirmation box appears on the PC; choosing "No" makes Claude stop and explain; choosing "Yes" means no further prompt in the same session.
4. Open a block-listed app; Claude's actions are refused.
5. During an operation, move the mouse to the top-left corner; the agent pauses and every following action is refused.
6. Disconnect the network and restore it; the tunnel reconnects automatically and Claude Code can keep calling.
7. Call `screenshot` while the screen is locked; it returns the "screen may be locked" hint.
8. Ask Claude to read a file whose content is a private key (generate a test key with `ssh-keygen`); it must be refused, even in bypass mode.
9. End-to-end task: "Open Notepad, write a 3-line meeting note, and save it to the desktop as test.txt".

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| **401 Unauthorized** | The tokens differ. Compare `WINPC_TOKEN` in the server's `settings.json` with `token` in Windows `config.json`; restart Claude Code after changing the environment variable |
| **421 Misdirected Request / Invalid Host** | The `Host` header is not in the allow-list. With Tailscale or a custom domain, add `IP:*` / `domain:*` to `allowed_hosts` |
| Connection refused / timeout | The agent is not running or the tunnel dropped. Check the output of the agent and tunnel windows on Windows; on the server `curl -i http://127.0.0.1:8765/mcp` should return 401 (the tunnel is up) |
| The tunnel reports `remote port forwarding failed` | (1) The port is already taken on the server (a previous connection was not released): use another `-RemotePort`, or retry after a moment; (2) the restricted key's `permitlisten` does not match `-RemotePort` |
| `Permission denied (publickey)` | The public key is not in the tunnel user's `authorized_keys`, or the file permissions/owner are wrong (directory 700, file 600, owned by that user) |
| **Clicks land in the wrong place** | Usually DPI awareness failed. The agent calls `SetProcessDpiAwareness(2)` at startup; make sure it is not being launched in compatibility mode; take a fresh `screenshot` before clicking, because coordinates must come from the latest screenshot (never from a `zoom` image) |
| **`type_text` drops or garbles characters in the new Notepad and other WinUI apps** (a run of characters becomes one repeated character, e.g. `OK abc`→`OOOK ccc`) | These apps stall while handling keys (especially around Enter), and the Unicode key events queued meanwhile get merged; **a longer delay does not fix it** (measured in 0.3.2). Use `type_text(method="paste")`: one `Ctrl+V` through the clipboard. A 237-character test string (emoji, every ASCII symbol, full-width Chinese punctuation) came out character-perfect and the original clipboard text was restored. A Chinese IME in Chinese mode makes it worse; switch it to English mode first |
| **Garbled text / missing characters** | `type_text` uses Unicode `SendInput` and is independent of the input method. If it still misbehaves, check whether the target program runs as administrator (UIPI silently drops input). For garbled text in PowerShell scripts, make sure the script is saved as **UTF-8 with BOM** (Windows PowerShell 5.1 reads UTF-8 without a BOM as GBK) |
| Black screenshot / "screen may be locked" | The screen is locked or the monitor is asleep; unlock and turn off auto-lock. A disconnected RDP session also goes black; run in the local console session |
| Tool timeouts | Slow tunnel or large screenshots. Raise `MCP_TOOL_TIMEOUT`; lower `max_long_edge` / `jpeg_quality` |
| "the target window runs as administrator" (in Chinese) | UIPI limitation. Restart the agent as administrator only if you really need to (higher risk); UAC prompts cannot be operated in any way |
| "paused by user" (in Chinese) | The emergency stop was triggered. Restart the agent on the Windows machine |
| A file tool says "sensitive location / private key / the agent's own directory" (in Chinese) | This is an intentional block and is never lifted in any mode. Do not try to work around it |
| The approval box did not appear / was denied | The box is an always-on-top dialog; no click within 60 s by default counts as "deny"; the agent must run in an interactive desktop session (not a service, not an SSH session) |
| `win-remote-on` says `win-remote.config.json` was not found | Copy the example and fill it in as described in 3.4 |

---

## 10. Upgrading, rotating the token, uninstalling

**Upgrade**

```powershell
cd "$HOME\winpc-remote"; git pull          # then win-remote-off and win-remote-on
```

```bash
claude plugin marketplace update winpc-remote && claude plugin update winpc@winpc-remote
```

Check the Windows-side version with `agent_version` from `get_status`.

**Rotate the token** (if it leaked, you suspect it leaked, or you change devices)

1. Windows: `win-remote-off`, edit `config.json` and set `"token"` to `""`, start again and note the new token;
2. Server: update `WINPC_TOKEN` in `~/.claude/settings.json` and restart Claude Code;
3. If the token ever appeared in a log, screenshot, chat or issue, clean those up too.

**Revoke the tunnel key**: delete its line from `/home/winpc-tunnel/.ssh/authorized_keys` on the server.

**Uninstall**

```bash
claude plugin uninstall winpc@winpc-remote && claude plugin marketplace remove winpc-remote
```

On Windows run `win-remote-off`, delete the scheduled task with `schtasks /Delete /TN WinPCAgent /F`, and delete the project directory.

---

## 11. Project layout, changes and license

```
windows-agent/   winpc_agent.py (MCP server, authorization, auth, audit) · backends.py (Windows / Fake backends)
                 fileops.py (file read/write and path policy) · start-agent.ps1 · start-tunnel.ps1 · win-remote.ps1 · bin/
claude-plugin/   Claude Code plugin winpc (.mcp.json + Skill + scripts/winpc_cp.py file-transfer script)
.claude-plugin/  marketplace.json (lets this repo be installed directly with `claude plugin marketplace add`)
tests/           unit + end-to-end tests (FakeBackend; run on Linux)
```

### Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full history (currently written in Chinese). The most recent entries:

- **0.3.6** fixed the agent process crashing when an approval box timed out (the box now runs in a separate subprocess);
- **0.3.5** added `expect_handle`: verifies a specific window handle, so several terminal windows can be told apart;
- **0.3.4** added `expect_app`: refuses to send keys when the foreground window is not the expected app (keys had landed in VS Code three times during testing);
- **0.3.3** fixed the intermittent `ECONNRESET` (uvicorn only keeps connections for 5 seconds by default); the transfer script now closes its MCP session on exit;
- **0.3.2** moved the approval box to the top-center of the screen so the Start menu no longer hides it; added `type_text(method="paste")`;
- **0.3.0** file transfer (binary, chunked, verified, resumable); **0.2.1** security hardening (private-key detection by content, etc.).

**Upgrading**: overwrite `windows-agent/` and restart the agent; `config.json` needs no changes (new options have defaults); on Linux run `claude plugin update winpc@winpc-remote`.

### Contributing

Issues and PRs are welcome. Run `pytest -q` before submitting. For changes to authorization or path policy, please add regression tests and describe the threat model in the PR. **Do not** paste tokens, private keys or private screenshot content into issues, PRs or logs.

### License

[MIT](LICENSE). The software is provided "as is", without warranty of any kind; you use it entirely at your own risk.

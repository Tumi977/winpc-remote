# 变更记录

本项目遵循语义化版本（0.x 阶段，接口可能调整）。

### 变更：0.3.5

- **`type_text` / `press_key` 新增 `expect_handle` 参数**：`expect_app` 只能按进程名校验，而 Agent 窗口、隧道窗口、你自己的终端都是 `windowsterminal.exe`，分不开。`expect_handle`（`list_windows` 返回的窗口句柄）按**具体窗口**校验。两者可以同时给。
- `winpc_cp.py`：网络中断（`RemoteDisconnected` / `IncompleteRead`，常见于隧道还在、Windows 上的 Agent 已退出或正在重启）现在给出清晰的错误和重试提示，而不是 Python traceback。
- 文档：说明“在开始菜单等会自动收起的浮层上点击授权框，浮层会被收起”（Windows 对任何点在它外面的点击都会收起，与窗口是否被激活无关）——Agent 会因窗口变了而拒绝发送并提示重试，应用已授权所以重试不会再弹框。

### 变更：0.3.4

- **`type_text` / `press_key` 新增 `expect_app` 参数**：声明要操作的应用（进程名，不区分大小写，`.exe` 可省略），前台窗口不是它就**拒绝发送，且先于任何授权弹框检查**。实测中，用户在两次调用之间切到别的窗口（点了别处、在 VS Code 里回答模型的提问），按键因此落进了 VS Code——同类事故发生三次（清空了一段未发送的草稿、两次把命令敲进聊天框）。Skill 现在要求每次发键都带 `expect_app`；不带时行为不变（向后兼容）。

### 变更：0.3.3

- **修复间歇性 `ECONNRESET`**：uvicorn 默认只保持空闲连接 5 秒，模型两次调用之间的思考常常更久，客户端复用已被服务器关闭的连接时，第一次调用就会 `ECONNRESET`（实机上几乎每次长时间思考后都会出现）。Agent 现在保持 600 秒（`KEEPALIVE_S`）。新增复现测试：对照组证明 uvicorn 默认配置会断连，修复组通过。
- **`winpc_cp.py` 退出时关闭 MCP 会话**（HTTP `DELETE`）。此前每次运行都在 Agent 里留下一个永不释放的会话（控制台刷满 `Created new transport with session ID`）。出错路径同样会关闭。
- **`bin/live_test.py` 增加前置检查**：新版记事本会恢复上次的标签页，而该脚本会向前台记事本输入并以“不保存”关闭它——如果你已有记事本窗口，会丢掉未保存的内容。现在检测到记事本窗口就在输入任何东西之前中止。

### 变更：0.3.2

- **授权确认框不再被开始菜单/搜索框遮住，也不再抢焦点。** 原来的系统消息框显示在屏幕正中，会被同样居中的开始菜单盖住（用户点不到“是”），而且弹出时会关掉开始菜单。现在改为屏幕**顶部居中**的置顶浮窗，带倒计时，**点击它不会激活、不会改变前台窗口**；tkinter 不可用或出任何问题时自动回退到系统消息框。
- **新增 `type_text(method="paste")`**：经剪贴板一次 `Ctrl+V` 粘贴，不受目标程序按键处理卡顿的影响。实机发现新版记事本在回车附近会卡顿，期间堆积的按键被合并成同一个字符（`OK abc`→`OOOK ccc`），逐字符发送也无法完全避免。粘贴会备份并恢复文本剪贴板；原剪贴板有非文本内容（图片、文件）时会在返回里提示已被替换。
- **回车后额外等待**（`type_newline_delay_ms`，默认 150ms）。实测这只能略微缓解，不能根治：新版记事本里 `method="keys"` 仍会丢字，所以长文本/多行文本请用 `method="paste"`。
- 修复：`get_file` 之外，剪贴板读取改为按 `GlobalSize` 有界读取并显式按 UTF-16 解码。
- 新增 14 项回归测试（含用模拟 Win32 剪贴板 API 验证“无论粘贴成败都恢复剪贴板”）。

### 变更：0.3.1（修复实机测试发现的两个缺陷）

- **`type_text` 在新版记事本里丢字/错字**：实测一次发送较长字符串时，整行会变成同一个字符重复（`type_text` → `tttttttttt`）。原因是一次 `SendInput` 塞进最多 100 个按键事件（约 50 个字符），应用来不及按序处理。现在逐字符发送并留间隔（`type_chunk_chars` / `type_delay_ms` 可调）；emoji 的两个 UTF-16 码元始终在同一次发送里。
- **确认框抢走焦点会导致“批准的是 A，按键却发给 B”**：实测批准 `searchhost.exe` 时，弹框关掉了开始菜单，输入落进了没有被授权的 VS Code。现在授权通过后（以及已授权应用在真正发送前）会**重新核对目标窗口**，窗口变了就中止、不发送任何输入，并提示重新 `screenshot`；应用已授权，重试不会再次弹框。
- 新增回归测试 `tests/test_typing_and_focus.py`（13 项），并用变异检查确认关掉复核后 4 项会失败。
- **从 0.3.0 升级**：覆盖 `windows-agent/` 后重启 Agent；`config.json` 不用改（新增项有默认值）。

### 变更：0.3.0（文件传输）

- 新增 MCP 工具 `stat_file` / `get_file` / `put_file`（二进制、分块、sha256 校验、原子替换、可续传）和 Linux 端脚本 `winpc_cp.py`（仅标准库，内容不经过模型上下文）。
- 新增配置项 `transfer_max_bytes`；新增 `tests/test_transfer.py` 与 `bin/live_transfer_test.py`。
- **从 0.2.x 升级**：覆盖 `windows-agent/` 后重启 Agent；`config.json` 不用改。Linux 端 `claude plugin update winpc@winpc-remote`。

### 变更：0.2.1（安全加固）

发布前的安全审计发现并修复：

- **按内容识别私钥**：此前私钥放在非常规路径、文件名不像 `id_rsa` 时，在 `all`/bypass 模式下可被读走。现在文件内容是私钥即拒绝读取和修改。
- **`--config` 指向别处时令牌文件可读**：此前只拦截 Agent 目录，现在配置文件本身无论在哪都被拦截。
- **扩充敏感文件名与路径**（`.env`、`.netrc`、`.npmrc`、`.git-credentials`、Brave/Opera/Vivaldi 数据、PowerShell 历史等）和**可执行扩展名**（`.jse .wsh .url .cpl .msc` 等）。
- `start-agent.ps1` 收紧 `config.json` 权限；`start-tunnel.ps1` 加 `IdentitiesOnly=yes`。
- `win-remote.ps1` 不再写死服务器信息，改读本地 `win-remote.config.json`，并改为 UTF-8 带 BOM。
- 文档：`claude mcp add --header` 改用单引号，避免令牌被明文写入 `~/.claude.json`；新增受限隧道密钥方案。

**从 0.2.0 升级**：覆盖 `windows-agent/` 下的文件即可，`config.json` 不用改；`win-remote.ps1` 现在需要先创建 `win-remote.config.json`（见 3.4）。

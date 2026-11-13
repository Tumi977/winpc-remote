# Changelog

This project follows semantic versioning (0.x: interfaces may still change).

### 0.4.0 (everything is now in English)

- **All user-visible text is English.** Tool results, error messages, the approval dialog (title, message, **Allow** / **Deny** buttons, countdown), tool descriptions, the server instructions, console output, the PowerShell scripts, the `winpc_cp.py` and live-test output, and the Claude Skill text were previously in Chinese; they are now English. The Chinese README (`README.zh-CN.md`) was removed.
- **Message formats changed** (relevant if you parse them): the screenshot text now reads `Foreground window: <process> '<title>'.`; `list_windows` lines read `handle=... | <process> | '<title>' | screen (l,t) WxH | screenshot coords ≈(x,y) WxH`; `list_files` rows are marked `[dir]` / `[file]`; `read_file` headers are `File <path> | encoding ... | newlines ... | ...`. `bin/live_test.py` was updated accordingly.
- Behavior is otherwise unchanged: same tools, same parameters, same security model. `config.json` needs no changes.
- **Upgrading from 0.3.x:** overwrite `windows-agent/`, restart the Agent, and run `claude plugin marketplace update winpc-remote && claude plugin update winpc@winpc-remote` on Linux. Confirm with `agent_version` from `get_status`.

### 0.3.6 (fix: an approval-dialog timeout could crash the Agent process)

- **The approval dialog now runs in a separate subprocess.** Since 0.3.2 the dialog used tkinter in an Agent worker thread; Tk requires an object to be destroyed in the thread that created it, and when the closed dialog was destroyed by garbage collection in another thread, Tcl reported `Tcl_AsyncDelete: async handler deleted by the wrong thread` and **terminated the whole Agent process**. On a real machine the Agent crashed exactly this way after a dialog timed out (60 s without a click; the console first showed a series of `invalid command name "...tick"`, then the process vanished). The dialog is now a `python backends.py --confirm-dialog` subprocess and the Agent only reads its result; a subprocess that crashes, hangs, cannot be started, or has no tkinter just falls back to a system message box or counts as a denial, and **no longer affects the Agent**.
- The subprocess **explicitly decodes its input bytes as `utf-8-sig`**: the subprocess self-test in the sync script on a real machine found that PowerShell 5.1 prepends a UTF-8 BOM when writing a string to a pipe (`json` parsing failed), and that `sys.stdin` decodes with the system code page by default (GBK on Chinese Windows), which garbles non-ASCII text in the dialog.
- New tests: a fake subprocess covers "choice / deny / garbled output / crash / abort / hang timeout / cannot start", and an AST check ensures `tkinter` only appears in the subprocess entry function.

### 0.3.5

- **`type_text` / `press_key` gained an `expect_handle` parameter**: `expect_app` can only check the process name, but the Agent window, the tunnel window and your own terminal are all `windowsterminal.exe` and cannot be told apart. `expect_handle` (the window handle returned by `list_windows`) checks the **specific window**. Both can be given together.
- `winpc_cp.py`: a network interruption (`RemoteDisconnected` / `IncompleteRead`, typical when the tunnel is still up but the Agent on Windows has exited or is restarting) now gives a clear error and a retry hint instead of a Python traceback.
- Docs: explained that "clicking the approval dialog over a popup that closes itself, such as the Start menu, closes that popup" (Windows closes it on any click outside it, whether or not a window is activated); the Agent refuses to send because the window changed and tells you to retry, and since the application is already approved the retry shows no dialog.

### 0.3.4

- **`type_text` / `press_key` gained an `expect_app` parameter**: declares the app to operate (process name, case-insensitive, `.exe` optional); when the foreground window is not it, the call is **refused and nothing is sent, before any approval dialog is checked**. In testing the user switched to another window between two calls (clicked elsewhere, answered the model's question in VS Code), so keys landed in VS Code; this happened three times (it cleared an unsent draft and twice typed commands into the chat box). The Skill now requires `expect_app` on every key-sending call; without it the behavior is unchanged (backward compatible).

### 0.3.3

- **Fixed the intermittent `ECONNRESET`**: uvicorn keeps idle connections for only 5 seconds by default, the model's thinking between two calls is often longer, and when the client reused a connection the server had already closed, the first call failed with `ECONNRESET` (it appeared after almost every long think on a real machine). The Agent now keeps connections for 600 seconds (`KEEPALIVE_S`). A reproduction test was added: the control group proves the uvicorn default drops the connection, the fixed group passes.
- **`winpc_cp.py` closes its MCP session on exit** (HTTP `DELETE`). Before, every run left a session in the Agent that was never released (the console filled with `Created new transport with session ID`). The error path closes it as well.
- **`bin/live_test.py` gained a pre-check**: the new Notepad restores the tabs of the last session, while that script types into the foreground Notepad and closes it with "Don't save"; if you already had a Notepad window, unsaved content would be lost. It now aborts before typing anything when it detects a Notepad window.

### 0.3.2

- **The approval dialog is no longer hidden by the Start menu / search box and no longer steals focus.** The old system message box appeared in the middle of the screen, where the equally centered Start menu covered it (the user could not click "Yes"), and showing it closed the Start menu. It is now a topmost floating window at the **top center** of the screen with a countdown, and **clicking it does not activate it or change the foreground window**; when tkinter is unavailable or anything goes wrong it falls back to the system message box automatically.
- **Added `type_text(method="paste")`**: one `Ctrl+V` paste through the clipboard, unaffected by the target app stalling on key handling. On a real machine the new Notepad stalled around Enter and the keys that piled up meanwhile were merged into one character (`OK abc` -> `OOOK ccc`), which even per-character sending could not fully avoid. The paste backs up and restores the text clipboard; when the original clipboard held non-text content (images, files), the result says it was replaced.
- **Extra wait after Enter** (`type_newline_delay_ms`, default 150 ms). In testing this only mitigates the problem and does not cure it: `method="keys"` still drops characters in the new Notepad, so use `method="paste"` for long or multi-line text.
- Fix: clipboard reads are now bounded by `GlobalSize` and explicitly decoded as UTF-16.
- Added 14 regression tests (including one that uses a simulated Win32 clipboard API to verify that the clipboard is restored whether the paste succeeds or fails).

### 0.3.1 (fixes two defects found in live testing)

- **`type_text` dropped/garbled characters in the new Notepad**: when a longer string was sent at once, the whole line turned into one repeated character in testing (`type_text` -> `tttttttttt`). The cause was one `SendInput` carrying up to 100 key events (about 50 characters), which the app could not process in order. Characters are now sent one by one with a delay (`type_chunk_chars` / `type_delay_ms` are adjustable); the two UTF-16 code units of an emoji always go in the same send.
- **The dialog stealing focus caused "A was approved, but the keys went to B"**: in testing, approving `searchhost.exe` made the dialog close the Start menu, and the input landed in an unauthorized VS Code. The target window is now **re-checked** after authorization (and before an already-approved application actually receives input); if the window changed, the call aborts, nothing is sent, and the result tells you to `screenshot` again; the application is already approved, so the retry shows no dialog again.
- Added the regression tests `tests/test_typing_and_focus.py` (13 tests); a mutation check confirmed that 4 of them fail when the re-check is switched off.
- **Upgrading from 0.3.0:** overwrite `windows-agent/` and restart the Agent; `config.json` needs no changes (new options have defaults).

### 0.3.0 (file transfer)

- Added the MCP tools `stat_file` / `get_file` / `put_file` (binary, chunked, sha256-verified, atomic replace, resumable) and the Linux-side script `winpc_cp.py` (standard library only; content never passes through the model context).
- Added the config option `transfer_max_bytes`; added `tests/test_transfer.py` and `bin/live_transfer_test.py`.
- **Upgrading from 0.2.x:** overwrite `windows-agent/` and restart the Agent; `config.json` needs no changes. On Linux run `claude plugin update winpc@winpc-remote`.

### 0.2.1 (security hardening)

Found and fixed by the pre-release security audit:

- **Private keys are detected by content**: before, a private key in an unusual path and with a name that did not look like `id_rsa` could be read away in `all`/bypass mode. Reading and modifying a file whose content is a private key is now refused.
- **The token file was readable when `--config` pointed elsewhere**: before, only the Agent directory was blocked; now the config file itself is blocked wherever it is.
- **Extended the sensitive file names and paths** (`.env`, `.netrc`, `.npmrc`, `.git-credentials`, Brave/Opera/Vivaldi data, PowerShell history, etc.) and the **executable extensions** (`.jse .wsh .url .cpl .msc`, etc.).
- `start-agent.ps1` tightens the permissions of `config.json`; `start-tunnel.ps1` adds `IdentitiesOnly=yes`.
- `win-remote.ps1` no longer hard-codes server information and reads the local `win-remote.config.json` instead, and was changed to UTF-8 with BOM.
- Docs: `claude mcp add --header` now uses single quotes so the token is not written to `~/.claude.json` in plain text; added a restricted tunnel-key setup.

**Upgrading from 0.2.0:** just overwrite the files under `windows-agent/`; `config.json` needs no changes; `win-remote.ps1` now needs `win-remote.config.json` created first (see 3.4).

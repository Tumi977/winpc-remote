<#
.SYNOPSIS
  一键启动/停止 WinPC Remote（Agent + SSH 反向隧道，各开一个窗口）。
.DESCRIPTION
  服务器连接信息读取同目录的 win-remote.config.json（已在 .gitignore 中，不会被提交）。
  首次使用：复制 win-remote.config.example.json 为 win-remote.config.json 并填写。
.EXAMPLE
  win-remote.ps1 on       # 普通模式：未授权应用首次操作会弹确认框
  win-remote.ps1 bypass   # 跳过应用授权确认（黑名单与敏感路径拦截仍生效）
  win-remote.ps1 off      # 停止 Agent 和隧道
#>
param(
    [Parameter(Mandatory)][ValidateSet('on', 'bypass', 'off')][string]$Mode
)
$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }

# 读取连接信息（off 模式不需要）
$cfgPath = Join-Path $PSScriptRoot 'win-remote.config.json'
$Port = 8765
$conn = $null
if (Test-Path $cfgPath) {
    $conn = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($conn.port) { $Port = [int]$conn.port }
}
elseif ($Mode -ne 'off') {
    throw "找不到 $cfgPath。请复制 win-remote.config.example.json 为 win-remote.config.json 并填写 server / user / key_file。"
}

function Stop-WinRemote {
    # 先停外层 PowerShell 循环（否则隧道会 5 秒后重连），再停 python / ssh
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and (
            ($_.Name -match '^powershell' -and $_.CommandLine -match 'start-(agent|tunnel)\.ps1') -or
            ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'winpc_agent\.py') -or
            ($_.Name -eq 'ssh.exe' -and $_.CommandLine -match "-R 127\.0\.0\.1:${Port}:")
        )
    }
    $order = @($procs | Where-Object { $_.Name -match '^powershell' }) +
             @($procs | Where-Object { $_.Name -notmatch '^powershell' })
    foreach ($p in $order) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    return $order.Count
}

$n = Stop-WinRemote
if ($Mode -eq 'off') {
    Write-Host "已停止 WinPC Remote（结束 $n 个进程）。"
    return
}
foreach ($k in 'server', 'user') {
    if (-not $conn.$k) { throw "win-remote.config.json 缺少字段: $k" }
}
if ($n -gt 0) { Write-Host "已停止旧的 Agent/隧道（$n 个进程）。"; Start-Sleep -Seconds 1 }

$agentArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start-agent.ps1'))
if ($Mode -eq 'bypass') { $agentArgs += '-BypassPermissions' }
Start-Process powershell -ArgumentList $agentArgs -WorkingDirectory $PSScriptRoot

$tunnelArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start-tunnel.ps1'),
                '-Server', $conn.server, '-User', $conn.user, '-Port', $Port)
if ($conn.key_file) { $tunnelArgs += @('-KeyFile', $conn.key_file) }
if ($conn.ssh_port) { $tunnelArgs += @('-SshPort', [int]$conn.ssh_port) }
Start-Process powershell -ArgumentList $tunnelArgs -WorkingDirectory $PSScriptRoot

$label = if ($Mode -eq 'bypass') { 'bypass（跳过授权确认）' } else { '普通（未授权应用会弹确认框）' }
Write-Host "已启动 Agent 和隧道，模式：$label。两个新窗口请保持打开。"
Write-Host "停止：win-remote-off"

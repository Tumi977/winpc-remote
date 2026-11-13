<#
.SYNOPSIS
  SSH 反向隧道（带自动重连）：把 Windows 本机的 agent 端口映射到 Linux 服务器的 127.0.0.1。
.EXAMPLE
  .\start-tunnel.ps1 -Server myserver.example.com -User alice
  .\start-tunnel.ps1 -Server 10.0.0.5 -User alice -KeyFile $HOME\.ssh\id_ed25519 -SshPort 2222
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Server,
    [Parameter(Mandatory)][string]$User,
    [int]$Port = 8765,          # Windows 上 agent 监听的端口
    [int]$RemotePort = 0,       # 服务器上暴露的端口，默认与 -Port 相同
    [int]$SshPort = 22,
    [string]$KeyFile = ''
)
$ErrorActionPreference = 'Continue'
if ($RemotePort -le 0) { $RemotePort = $Port }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw '未找到 ssh。请在“设置 → 可选功能”里安装 OpenSSH 客户端。'
}

$sshArgs = @(
    '-N',
    '-R', "127.0.0.1:${RemotePort}:127.0.0.1:${Port}",   # 只绑定服务器回环地址
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-p', $SshPort
)
if ($KeyFile) { $sshArgs += @('-i', $KeyFile, '-o', 'IdentitiesOnly=yes') }
$sshArgs += "$User@$Server"

while ($true) {
    Write-Host ("[{0}] 连接 {1}@{2}，转发 服务器 127.0.0.1:{3} → 本机 127.0.0.1:{4}" -f (Get-Date -Format 'HH:mm:ss'), $User, $Server, $RemotePort, $Port)
    & ssh @sshArgs
    Write-Host ("[{0}] 隧道断开（退出码 {1}），5 秒后重连 ..." -f (Get-Date -Format 'HH:mm:ss'), $LASTEXITCODE)
    Start-Sleep -Seconds 5
}

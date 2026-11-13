<#
.SYNOPSIS
  SSH reverse tunnel (with automatic reconnect): maps the agent port on this Windows machine to 127.0.0.1 on the Linux server.
.EXAMPLE
  .\start-tunnel.ps1 -Server myserver.example.com -User alice
  .\start-tunnel.ps1 -Server 10.0.0.5 -User alice -KeyFile $HOME\.ssh\id_ed25519 -SshPort 2222
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Server,
    [Parameter(Mandatory)][string]$User,
    [int]$Port = 8765,          # port the agent listens on, on Windows
    [int]$RemotePort = 0,       # port exposed on the server; defaults to the same as -Port
    [int]$SshPort = 22,
    [string]$KeyFile = ''
)
$ErrorActionPreference = 'Continue'
if ($RemotePort -le 0) { $RemotePort = $Port }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw 'ssh not found. Install the OpenSSH client under Settings > Optional features.'
}

$sshArgs = @(
    '-N',
    '-R', "127.0.0.1:${RemotePort}:127.0.0.1:${Port}",   # bind only the server loopback address
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-p', $SshPort
)
if ($KeyFile) { $sshArgs += @('-i', $KeyFile, '-o', 'IdentitiesOnly=yes') }
$sshArgs += "$User@$Server"

while ($true) {
    Write-Host ("[{0}] Connecting {1}@{2}, forwarding server 127.0.0.1:{3} -> this machine 127.0.0.1:{4}" -f (Get-Date -Format 'HH:mm:ss'), $User, $Server, $RemotePort, $Port)
    & ssh @sshArgs
    Write-Host ("[{0}] Tunnel disconnected (exit code {1}), reconnecting in 5 s ..." -f (Get-Date -Format 'HH:mm:ss'), $LASTEXITCODE)
    Start-Sleep -Seconds 5
}

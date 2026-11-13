<#
.SYNOPSIS
  Start/stop WinPC Remote with one command (Agent + SSH reverse tunnel, each in its own window).
.DESCRIPTION
  The server connection details are read from win-remote.config.json in the same directory (it is in .gitignore and will not be committed).
  First use: copy win-remote.config.example.json to win-remote.config.json and fill it in.
.EXAMPLE
  win-remote.ps1 on       # normal mode: the first operation on an unapproved app shows a confirmation dialog
  win-remote.ps1 bypass   # skip application approval prompts (the block-list and sensitive-path blocking still apply)
  win-remote.ps1 off      # stop the Agent and the tunnel
#>
param(
    [Parameter(Mandatory)][ValidateSet('on', 'bypass', 'off')][string]$Mode
)
$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }

# Read the connection details (not needed in off mode)
$cfgPath = Join-Path $PSScriptRoot 'win-remote.config.json'
$Port = 8765
$conn = $null
if (Test-Path $cfgPath) {
    $conn = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($conn.port) { $Port = [int]$conn.port }
}
elseif ($Mode -ne 'off') {
    throw "Cannot find $cfgPath. Copy win-remote.config.example.json to win-remote.config.json and fill in server / user / key_file."
}

function Stop-WinRemote {
    # Stop the outer PowerShell loops first (otherwise the tunnel would reconnect after 5 s), then stop python / ssh
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
    Write-Host "Stopped WinPC Remote ($n process(es) ended)."
    return
}
foreach ($k in 'server', 'user') {
    if (-not $conn.$k) { throw "win-remote.config.json is missing the field: $k" }
}
if ($n -gt 0) { Write-Host "Stopped the old Agent/tunnel ($n process(es))."; Start-Sleep -Seconds 1 }

$agentArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start-agent.ps1'))
if ($Mode -eq 'bypass') { $agentArgs += '-BypassPermissions' }
Start-Process powershell -ArgumentList $agentArgs -WorkingDirectory $PSScriptRoot

$tunnelArgs = @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'start-tunnel.ps1'),
                '-Server', $conn.server, '-User', $conn.user, '-Port', $Port)
if ($conn.key_file) { $tunnelArgs += @('-KeyFile', $conn.key_file) }
if ($conn.ssh_port) { $tunnelArgs += @('-SshPort', [int]$conn.ssh_port) }
Start-Process powershell -ArgumentList $tunnelArgs -WorkingDirectory $PSScriptRoot

$label = if ($Mode -eq 'bypass') { 'bypass (approval prompts skipped)' } else { 'normal (unapproved apps show a confirmation dialog)' }
Write-Host "Started the Agent and the tunnel, mode: $label. Keep the two new windows open."
Write-Host "To stop: win-remote-off"

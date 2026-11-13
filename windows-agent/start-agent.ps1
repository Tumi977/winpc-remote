<#
.SYNOPSIS
  Start winpc-agent: detects Python, creates the venv, installs dependencies, and generates a token on first run.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
#>
[CmdletBinding()]
param(
    [string]$Config = '',
    [ValidateSet('windows', 'fake')][string]$Backend = 'windows',
    [switch]$BypassPermissions   # skip all application approval prompts (the block-list still applies)
)
$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Config) { $Config = Join-Path $PSScriptRoot 'config.json' }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location $PSScriptRoot

# 1. Find Python 3.10+
$pyExe = $null
$pyArgs = @()
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = $cand[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $extra = @($cand | Select-Object -Skip 1)
        $ok = & $exe @extra -c "import sys; print(int(sys.version_info >= (3,10)))" 2>$null
        if ($ok -eq '1') { $pyExe = $exe; $pyArgs = $extra; break }
    }
}
if (-not $pyExe) { throw 'Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick Add to PATH).' }

# 2. venv + dependencies (reinstalled only when requirements.txt changes)
$venv = Join-Path $PSScriptRoot '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Creating virtual environment .venv ...'
    & $pyExe @pyArgs -m venv $venv
}
$req = Join-Path $PSScriptRoot 'requirements.txt'
$stamp = Join-Path $venv '.requirements.sha256'
$hash = (Get-FileHash $req -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
    Write-Host 'Installing dependencies ...'
    & $venvPy -m pip install --upgrade pip | Out-Null
    & $venvPy -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    Set-Content -Path $stamp -Value $hash
}

# 3. Config: copied from the example on first run; when the token is empty the agent generates one and prints it only once
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $Config
    Write-Host "Created config file $Config (edit allowed_apps / approval_mode etc.)."
}

# 4. Tighten access to config.json: it stores the access token in plain text, so only the current user may read/write it (inheritance is removed,
#    otherwise, when it sits in a non-user directory such as D:\ or E:\, other accounts on the same machine might read it). A failure only warns and does not stop startup.
try {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Config /inheritance:r /grant:r ("{0}:(R,W)" -f $me) | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "Failed to tighten permissions on $Config (icacls exit code $LASTEXITCODE); please check manually." }
} catch { Write-Warning "Failed to tighten permissions on ${Config}: $_" }

Write-Host 'Starting winpc-agent (Ctrl+C to quit; emergency stop: move the mouse to the top-left corner of the primary screen) ...'
$agentArgs = @('--backend', $Backend, '--config', $Config)
if ($BypassPermissions) { $agentArgs += '--bypass-permissions' }
& $venvPy (Join-Path $PSScriptRoot 'winpc_agent.py') @agentArgs
exit $LASTEXITCODE

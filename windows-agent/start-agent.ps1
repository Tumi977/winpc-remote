<#
.SYNOPSIS
  启动 winpc-agent：检测 Python、创建 venv、安装依赖、首次运行自动生成 token。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\start-agent.ps1
#>
[CmdletBinding()]
param(
    [string]$Config = '',
    [ValidateSet('windows', 'fake')][string]$Backend = 'windows',
    [switch]$BypassPermissions   # 跳过所有应用授权确认（黑名单仍生效）
)
$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) { $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Config) { $Config = Join-Path $PSScriptRoot 'config.json' }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location $PSScriptRoot

# 1. 找 Python 3.10+
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
if (-not $pyExe) { throw '未找到 Python 3.10+。请从 https://www.python.org/downloads/ 安装（勾选 Add to PATH）。' }

# 2. venv + 依赖（requirements.txt 变化时才重装）
$venv = Join-Path $PSScriptRoot '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '创建虚拟环境 .venv ...'
    & $pyExe @pyArgs -m venv $venv
}
$req = Join-Path $PSScriptRoot 'requirements.txt'
$stamp = Join-Path $venv '.requirements.sha256'
$hash = (Get-FileHash $req -Algorithm SHA256).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
    Write-Host '安装依赖 ...'
    & $venvPy -m pip install --upgrade pip | Out-Null
    & $venvPy -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
    Set-Content -Path $stamp -Value $hash
}

# 3. 配置：首次从示例复制；token 为空时 agent 会自动生成并只打印一次
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $Config
    Write-Host "已生成配置文件 $Config（可编辑 allowed_apps / approval_mode 等）。"
}

# 4. 收紧 config.json 的访问权限：它以明文保存访问令牌，只允许当前用户读写（取消继承，
#    否则放在 D:\、E:\ 等非用户目录时，同机其他账户可能读到）。失败只警告，不影响启动。
try {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Config /inheritance:r /grant:r ("{0}:(R,W)" -f $me) | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "收紧 $Config 的权限失败（icacls 退出码 $LASTEXITCODE），请手动检查。" }
} catch { Write-Warning "收紧 $Config 的权限失败：$_" }

Write-Host '启动 winpc-agent（Ctrl+C 退出；紧急停止：把鼠标甩到主屏左上角）...'
$agentArgs = @('--backend', $Backend, '--config', $Config)
if ($BypassPermissions) { $agentArgs += '--bypass-permissions' }
& $venvPy (Join-Path $PSScriptRoot 'winpc_agent.py') @agentArgs
exit $LASTEXITCODE

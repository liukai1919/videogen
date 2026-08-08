# 启动 VideoTube Videogen(Windows PowerShell)。
#
#   .\start.ps1              # 装好依赖并启动，默认 8020
#   .\start.ps1 -Check       # 只自检不启动
#   .\start.ps1 -Port 8030
#
# 脚本是幂等的:虚拟环境、依赖和 config.yaml 都是缺什么补什么，
# 已经就位就直接启动。改过 pyproject.toml 后会自动重装依赖。

[CmdletBinding()]
param(
    [int]$Port,
    [string]$Config = "config.yaml",
    [switch]$Check,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$stamp = Join-Path $venv ".deps-stamp"

if (-not (Test-Path $python)) {
    Write-Host "==> 创建虚拟环境 .venv" -ForegroundColor Cyan
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3.11 -m venv $venv } else { & python -m venv $venv }
    if (-not (Test-Path $python)) { throw "创建虚拟环境失败，确认装了 Python 3.11+" }
}

# pyproject 比上次安装新，就说明依赖表变了(比如新增的 yt-dlp)。
$projectFile = Join-Path $PSScriptRoot "pyproject.toml"
$needsInstall = $Reinstall -or -not (Test-Path $stamp) -or
    ((Get-Item $projectFile).LastWriteTimeUtc -gt (Get-Item $stamp).LastWriteTimeUtc)

if ($needsInstall) {
    Write-Host "==> 安装依赖" -ForegroundColor Cyan
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
    New-Item -ItemType File -Path $stamp -Force | Out-Null
}

if (-not (Test-Path $Config)) {
    Write-Host "==> 没有 $Config，从 config.example.yaml 复制一份" -ForegroundColor Yellow
    Copy-Item "config.example.yaml" $Config
}

$arguments = @("-m", "videogen_service.cli", "--config", $Config)
if ($PSBoundParameters.ContainsKey("Port")) { $arguments += @("--port", $Port) }
if ($Check) { $arguments += "--check" }

& $python @arguments
exit $LASTEXITCODE

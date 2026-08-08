# 在 Windows 上一键把本服务启动到 WSL 里。
#
#   .\start-wsl.ps1                 # 启动，默认 8020
#   .\start-wsl.ps1 -Check          # 只自检不启动
#   .\start-wsl.ps1 -Port 8030
#   .\start-wsl.ps1 -Distro Ubuntu  # 指定发行版
#
# 做的事:把本仓库的 Windows 路径翻成 WSL 路径，进到那里，把 start.sh
# 去掉 CR 之后喂给 bash。之所以走管道而不是直接执行，是因为这个仓库被
# 两个平台共用:Windows 上的 checkout 很可能是 CRLF，而 CRLF 的脚本在
# bash 里会以 $'\r': command not found 失败——那个报错完全看不出病因。
# 管道的写法既不改动文件，也不要求文件有可执行位。

[CmdletBinding()]
param(
    [int]$Port,
    [string]$Config,
    [string]$Distro,
    [switch]$Check,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Error "没找到 wsl.exe。先装 WSL:以管理员身份运行 wsl --install"
    exit 1
}

# 所有对 wsl.exe 的调用都带上同一组发行版参数。
$wslArgs = @()
if ($Distro) { $wslArgs += @("-d", $Distro) }

$wslPath = (& wsl.exe @wslArgs wslpath -a "$PSScriptRoot" 2>$null | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or -not $wslPath) {
    Write-Error "WSL 起不来，或者翻译不了路径 $PSScriptRoot。先跑一次 wsl --list --verbose 看看发行版状态"
    exit 1
}
$wslPath = $wslPath.Trim()

# 转发给 start.sh 的参数。
$forward = @()
if ($PSBoundParameters.ContainsKey("Port")) { $forward += @("--port", "$Port") }
if ($Config) { $forward += @("--config", $Config) }
if ($Check) { $forward += "--check" }
if ($Reinstall) { $forward += "--reinstall" }

# 单引号包起来交给 bash，路径里有空格也不会散架。
$quoted = ($forward | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join " "
$repo = "'" + ($wslPath -replace "'", "'\''") + "'"
$command = "cd $repo && tr -d '\r' < start.sh | bash -s -- $quoted"

Write-Host "==> WSL: $wslPath" -ForegroundColor Cyan
& wsl.exe @wslArgs -- bash -lc $command
exit $LASTEXITCODE

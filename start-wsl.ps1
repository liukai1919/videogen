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
#
# 这个文件必须存成带 BOM 的 UTF-8:Windows PowerShell 5.1 会用 ANSI
# (中文系统上是 GBK)读没有 BOM 的 .ps1，中文注释解错后会吞掉引号。

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

# 用单引号包给 bash:反斜杠、空格在单引号里都是字面量。
function Quote-ForBash([string]$Value) {
    return "'" + ($Value -replace "'", "'\''") + "'"
}

# 原生命令往 stderr 写一行就被 $ErrorActionPreference = "Stop" 当成终止
# 错误——wsl 的提示、uvicorn 的日志全在 stderr，所以调它们时必须让开。
function Invoke-Wsl([string[]]$Arguments, [switch]$Capture) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Capture) { return (& wsl.exe @Arguments 2>&1) }
        & wsl.exe @Arguments
        return $null
    } finally {
        $ErrorActionPreference = $previous
    }
}

# wslpath 必须隔着 bash 的单引号传:直接 `wsl wslpath -a D:\repo` 会把
# 反斜杠当转义吃掉，wslpath 收到的是 "D:repo"。
$probe = Invoke-Wsl (
    $wslArgs + @("--", "bash", "-lc", "wslpath -a $(Quote-ForBash $PSScriptRoot)")
) -Capture
$wslPath = $probe |
    Where-Object { $_ -is [string] -and $_.Trim().StartsWith("/") } |
    Select-Object -First 1

if ($wslPath) {
    $wslPath = $wslPath.Trim()
} elseif ($PSScriptRoot -match '^([A-Za-z]):\\(.*)$') {
    # wslpath 不在或者报错时的兜底:盘符路径的翻译规则是固定的。
    $wslPath = "/mnt/" + $Matches[1].ToLower() + "/" + ($Matches[2] -replace '\\', '/')
    Write-Host "==> wslpath 没给出结果，按 /mnt 规则翻译" -ForegroundColor Yellow
} else {
    Write-Error "翻译不了路径 $PSScriptRoot。先跑 wsl --list --verbose 看看发行版状态,$probe"
    exit 1
}

# 转发给 start.sh 的参数。
$forward = @()
if ($PSBoundParameters.ContainsKey("Port")) { $forward += @("--port", "$Port") }
if ($Config) { $forward += @("--config", $Config) }
if ($Check) { $forward += "--check" }
if ($Reinstall) { $forward += "--reinstall" }

$quoted = ($forward | ForEach-Object { Quote-ForBash $_ }) -join " "
$command = "cd $(Quote-ForBash $wslPath) && tr -d '\r' < start.sh | bash -s -- $quoted"

Write-Host "==> WSL: $wslPath" -ForegroundColor Cyan
Invoke-Wsl ($wslArgs + @("--", "bash", "-lc", $command))
exit $LASTEXITCODE

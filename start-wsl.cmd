@echo off
rem 双击就能用的入口:绕开 PowerShell 的执行策略，直接调用 start-wsl.ps1。
rem 命令行参数照样透传: start-wsl.cmd -Check / -Port 8030
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-wsl.ps1" %*
rem 双击运行时窗口会在出错后立刻关掉，什么都看不到，所以失败才停一下。
if errorlevel 1 pause

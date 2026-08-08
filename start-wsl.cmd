@echo off
rem Double-click entry point: runs start-wsl.ps1 with the execution policy bypassed.
rem Arguments pass straight through:  start-wsl.cmd -Check   /   start-wsl.cmd -Port 8030
rem
rem Kept strictly ASCII on purpose. cmd.exe reads a .bat/.cmd in the console's OEM
rem code page (936 on a Chinese Windows), so UTF-8 Chinese here is re-read as GBK
rem and the mangled bytes can contain 0x26 "&", which ends the rem and runs the
rem rest as a command. A comment must never be able to do that.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-wsl.ps1" %*
rem A double-clicked window closes instantly on failure, so hold it open to read the error.
if errorlevel 1 pause

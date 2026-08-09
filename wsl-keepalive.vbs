' wsl-keepalive.vbs -- keep the WSL2 distro (and the videogen/comfyui
' systemd services inside it) alive.
'
' Why: on this machine WSL2 terminates the whole distro container some
' 15-60s after the LAST wsl.exe client detaches, even though the utility
' VM itself stays up (vmIdleTimeout=-1). systemd and every service inside
' die silently with it. Keeping one wsl.exe client attached prevents that.
'
' The old approach (a single "wsl.exe -e sleep infinity" from a ONCE
' scheduled task) was a single point of failure: when that process died
' (e.g. during the 2026-08-09 OOM storm) protection was silently lost.
' This script loops: if the attachment dies for ANY reason it re-attaches
' 5 seconds later, which also boots the distro again and systemd brings
' the services back up.
'
' Deployment (both, they are redundant on purpose):
'   1. scheduled task "wsl-keepalive" runs:  wscript.exe <this file>
'      (arm immediately after a reboot:  schtasks /Run /TN wsl-keepalive)
'   2. a copy lives in shell:startup so every logon re-arms it hidden.
'
' Log: D:\vediotube-videogen\logs\wsl-keepalive.log

Const LOGFILE = "D:\vediotube-videogen\logs\wsl-keepalive.log"

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

WriteLog "keepalive started (pid host: wscript)"

Do
    WriteLog "attaching wsl.exe --exec sleep infinity"
    rc = sh.Run("wsl.exe --exec sleep infinity", 0, True)
    WriteLog "wsl.exe exited rc=" & rc & ", re-attach in 5s"
    WScript.Sleep 5000
Loop

Sub WriteLog(msg)
    On Error Resume Next
    Dim f
    Set f = fso.OpenTextFile(LOGFILE, 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
End Sub

Option Explicit

Const APP_DIR = "C:\Users\congtrun\Documents\PCS1STOP"

Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
StopRunningApp
WshShell.Run """" & APP_DIR & "\pcs1stop.bat""", 0, False

' Stops what an earlier start left running: the hidden cmd.exe that runs pcs1stop.bat and any python.exe of this
' project's .venv that runs app.py.  taskkill /T takes their children along (uv.exe, the real python.exe behind the
' venv launcher).  Other python / cmd processes (VS Code, other projects) are left alone.
Sub StopRunningApp()
    Dim wmi, procs, proc, cmdLine, ids, id, appPython, appBat, appPy
    On Error Resume Next
    appPython = LCase(APP_DIR & "\.venv\Scripts\python.exe")
    appBat = LCase(APP_DIR & "\pcs1stop.bat")
    Set appPy = New RegExp
    appPy.Pattern = "\sapp\.py(\s|$)"
    Set ids = CreateObject("Scripting.Dictionary")
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    If Err.Number <> 0 Then Exit Sub
    Set procs = wmi.ExecQuery("SELECT ProcessId, Name, ExecutablePath, CommandLine FROM Win32_Process " & _
                              "WHERE Name = 'cmd.exe' OR Name = 'python.exe'")
    If Err.Number <> 0 Then Exit Sub
    For Each proc In procs
        cmdLine = LCase("" & proc.CommandLine)
        If LCase(proc.Name) = "cmd.exe" Then
            If InStr(cmdLine, appBat) > 0 Then ids.Add proc.ProcessId, True
        ElseIf LCase("" & proc.ExecutablePath) = appPython And appPy.Test(cmdLine) Then
            ids.Add proc.ProcessId, True
        End If
    Next
    For Each id In ids.Keys
        WshShell.Run "taskkill /F /T /PID " & id, 0, True
    Next
    If ids.Count > 0 Then WScript.Sleep 1000
End Sub

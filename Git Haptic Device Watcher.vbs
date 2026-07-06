Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = projectDir & "\.venv\Scripts\pythonw.exe"
watcher = projectDir & "\device_watcher.py"
logPath = projectDir & "\watcher_error.log"

If Not fso.FileExists(pythonw) Then
    Set logFile = fso.CreateTextFile(logPath, True)
    logFile.WriteLine "pythonw.exe was not found: " & pythonw
    logFile.Close
    MsgBox "Could not find pythonw.exe. See watcher_error.log in the project folder.", 16, "Git Haptic Watcher"
    WScript.Quit 1
End If

If Not fso.FileExists(watcher) Then
    Set logFile = fso.CreateTextFile(logPath, True)
    logFile.WriteLine "device_watcher.py was not found: " & watcher
    logFile.Close
    MsgBox "Could not find device_watcher.py. See watcher_error.log in the project folder.", 16, "Git Haptic Watcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectDir
shell.Run """" & pythonw & """ """ & watcher & """", 0, False

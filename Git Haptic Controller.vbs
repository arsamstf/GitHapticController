Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = projectDir & "\.venv\Scripts\pythonw.exe"
app = projectDir & "\git_haptic_app.py"
logPath = projectDir & "\launcher_error.log"

If Not fso.FileExists(pythonw) Then
    Set logFile = fso.CreateTextFile(logPath, True)
    logFile.WriteLine "pythonw.exe was not found: " & pythonw
    logFile.Close
    MsgBox "Could not find pythonw.exe. See launcher_error.log in the project folder.", 16, "Git Haptic Controller"
    WScript.Quit 1
End If

If Not fso.FileExists(app) Then
    Set logFile = fso.CreateTextFile(logPath, True)
    logFile.WriteLine "App file was not found: " & app
    logFile.Close
    MsgBox "Could not find git_haptic_app.py. See launcher_error.log in the project folder.", 16, "Git Haptic Controller"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectDir
shell.Run """" & pythonw & """ """ & app & """", 0, False

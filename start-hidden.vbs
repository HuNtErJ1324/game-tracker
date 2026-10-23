' Starts Game Tracker with no console window (handy for Windows startup).
'   start-hidden.vbs          run everything on this PC
'   start-hidden.vbs agent    run only the tracker, reporting to your saved home server
' Output goes to data\gametracker.log.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
mode = ""
If WScript.Arguments.Count > 0 Then mode = WScript.Arguments(0)
sh.Run "cmd /c .\start.bat --no-browser --log data\gametracker.log " & mode, 0, False

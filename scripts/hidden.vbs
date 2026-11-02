' Uruchamia program bez okna konsoli: wscript.exe hidden.vbs program argument1 argument2 ...
' (uzywane przez start.bat, zeby serwer w WSL dzialal w tle, bez terminala)
Option Explicit
Dim sh, cmd, i
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  If i > 0 Then cmd = cmd & " "
  cmd = cmd & """" & Replace(WScript.Arguments(i), """", "\""") & """"
Next
sh.Run cmd, 0, False

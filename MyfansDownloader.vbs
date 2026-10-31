' Double-click launcher. Starts MyfansDownloader.bat from this folder.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run """" & folder & "\MyfansDownloader.bat""", 1, False

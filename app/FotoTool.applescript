-- Starter für FotoTool. Eigene App, damit Festplattenzugriff nur hierfür freigegeben werden muss.
on run
	set appPath to POSIX path of (path to me)
	set toolDir to do shell script "dirname " & quoted form of appPath
	set py to "/opt/homebrew/bin/python3"
	try
		do shell script "test -x " & py
	on error
		set py to "/usr/bin/python3"
	end try
	try
		do shell script "cd " & quoted form of toolDir & " && " & py & " -m fototool 2>&1"
	on error errMsg
		display dialog "FotoTool konnte nicht starten:" & return & return & errMsg buttons {"OK"} default button 1 with icon stop
	end try
end run

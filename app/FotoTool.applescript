-- Starter für FotoTool. Eigene App, damit Festplattenzugriff nur hierfür freigegeben werden muss.
-- Die eigentliche Logik steht in app/start.sh (änderbar ohne Neubau der App).
on run
	set appPath to POSIX path of (path to me)
	set toolDir to do shell script "dirname " & quoted form of appPath
	try
		do shell script "/bin/zsh " & quoted form of (toolDir & "/app/start.sh") & " 2>&1"
	on error errMsg
		display dialog "FotoTool konnte nicht starten:" & return & return & errMsg buttons {"OK"} default button 1 with icon stop
	end try
end run

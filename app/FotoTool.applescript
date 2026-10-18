-- Starter für FotoTool. Eigene App, damit Festplattenzugriff nur hierfür freigegeben werden muss.
-- Die eigentliche Logik steht in app/start.sh (änderbar ohne Neubau der App).
-- Startet Python im Hintergrund und bleibt (ohne Dock-Symbol) geöffnet, bis FotoTool beendet ist:
-- macOS rechnet den Festplattenzugriff von Python dieser App zu. Würde sie blockierend auf Python
-- warten, zeigte „Sofort beenden“ sie als „reagiert nicht“.
-- Keine properties/globals: das Applet würde sie beim Beenden speichern und so die Signatur brechen.

on logDir()
	return POSIX path of (path to home folder) & ".fototool/logs"
end logDir

on run
	set appPath to POSIX path of (path to me)
	set toolDir to do shell script "dirname " & quoted form of appPath
	set d to logDir()
	do shell script "mkdir -p " & quoted form of d & "; rm -f " & quoted form of (d & "/start.status") & ¬
		"; (/bin/zsh " & quoted form of (toolDir & "/app/start.sh") & " > " & quoted form of (d & "/start.log") & ¬
		" 2>&1; echo $? > " & quoted form of (d & "/start.status") & ") > /dev/null 2>&1 &"
end run

on idle
	set d to logDir()
	set exitCode to do shell script "cat " & quoted form of (d & "/start.status") & " 2>/dev/null || echo running"
	if exitCode is "running" then return 2
	if exitCode is not "0" then
		set msg to do shell script "tail -n 15 " & quoted form of (d & "/start.log")
		activate
		display dialog "FotoTool wurde mit Fehler beendet:" & return & return & msg buttons {"OK"} default button 1 with icon stop
	end if
	quit
	return 2
end idle

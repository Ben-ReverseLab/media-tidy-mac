-- Launcher for MediaTidy. A separate app, so that disk access only has to be granted to it.
-- The actual logic is in app/start.sh (can be changed without rebuilding the app).
-- Starts Python in the background and stays open (without a Dock icon) until MediaTidy has quit:
-- macOS attributes Python's disk access to this app. If it waited for Python in a blocking way,
-- "Force Quit" would show it as "not responding".
-- No properties/globals: the applet would save them when quitting and so break its signature.

on logDir()
	return POSIX path of (path to home folder) & ".mediatidy/logs"
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
		set title to "MediaTidy quit with an error:"
		if user locale of (system info) starts with "de" then set title to "MediaTidy wurde mit Fehler beendet:"
		activate
		display dialog title & return & return & msg buttons {"OK"} default button 1 with icon stop
	end if
	quit
	return 2
end idle

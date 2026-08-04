@echo off
rem ASCII only: .cmd is read in the OEM codepage, Cyrillic here breaks.
rem Starts the voice input daemon without a console window.
rem   CapsLock   - start/stop dictation (LED on = recording)
rem   Alt+X      - cancel
rem   Ctrl+Alt+Q - quit daemon
rem Also starts on Windows logon: shortcut in the Startup folder.
setlocal
cd /d "%~dp0"

rem The daemon writes _daemon.log itself, so no redirection here: with both,
rem every line landed in the file twice. Logging must not depend on how it starts.
start "" /b pythonw voice_hotkey.py

echo Voice input daemon started.
echo   CapsLock  start/stop     Alt+X  cancel     Ctrl+Alt+Q  quit
echo   Log:    %~dp0_daemon.log
echo   Status: %~dp0_last_run.status
timeout /t 4 >nul

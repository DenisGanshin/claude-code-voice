@echo off
rem ASCII only: .cmd is read in the OEM codepage, Cyrillic here breaks.
rem Turns the voice input OFF and keeps it off.
rem
rem Why a file next to the tray icon: Windows 11 hides new tray icons under the
rem overflow arrow, so the icon alone is not a visible switch until the user
rem drags it onto the taskbar. This script always works.
rem
rem Creates _daemon.off so the keepalive task does NOT bring the daemon back.
rem To turn it on again: start_hotkey.cmd (it removes the flag).
setlocal
cd /d "%~dp0"

echo turned off by stop_hotkey.cmd > _daemon.off
echo see start_hotkey.cmd to turn it back on >> _daemon.off

python voice_hotkey.py --send quit >nul 2>&1

echo Voice input is OFF.
echo   The keepalive task will NOT restart it while _daemon.off exists.
echo   Turn it back on: start_hotkey.cmd
rem 2>nul as well: with stdin redirected (scripted run) timeout prints an error
rem instead of waiting, and that error is the only thing the caller sees.
timeout /t 4 >nul 2>nul

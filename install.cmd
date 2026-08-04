@echo off
rem ASCII only: .cmd is read in the OEM codepage, Cyrillic here breaks.
rem Thin wrapper: the real work lives in install.py, where quoting, encoding
rem and path substitution behave predictably.
setlocal
cd /d "%~dp0"

python install.py %*
if errorlevel 1 (
    echo.
    echo Setup did not finish. Check that "python" is on PATH ^(python --version^).
    pause
    exit /b 1
)
pause

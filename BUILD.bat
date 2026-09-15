@echo off
REM Build the single-file executable. Needs: pip install pyinstaller flask
REM
REM templates\, static\ and reports\ are bundled, so the captured report bodies
REM travel with the exe. data\ is not: it is written next to the exe at runtime.

pyinstaller --onefile --name LabKPIs ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "reports;reports" ^
  app.py

echo.
echo Built dist\LabKPIs.exe

REM Wait for a keypress when a person ran this by double-clicking, so the
REM window does not vanish before they read it. CI is always set on a runner
REM and never set on a desktop, so the workflow runs this same script without
REM sitting on a prompt nobody can answer.
if not defined CI pause

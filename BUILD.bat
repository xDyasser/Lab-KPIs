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
pause

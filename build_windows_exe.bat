@echo off
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller
py -3 -m PyInstaller --onefile --windowed --name ImageApiClient --add-data ".env.example;." web_app.py
echo.
echo Done. The exe is in the dist folder.
pause

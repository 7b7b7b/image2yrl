@echo off
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller
py -3 -m PyInstaller --onefile --windowed --name ImageApiClient image_gui.py
echo.
echo Done. The exe is in the dist folder.
pause

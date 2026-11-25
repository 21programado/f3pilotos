@echo off
rem Genera dist\Carrera4.exe (ejecutar en una PC con Windows que tenga Python)
if not exist static\socket.io.min.js (
  echo Falta static\socket.io.min.js: copialo desde la carpeta static del juego de tenis.
  pause
  exit /b 1
)
pip install flask flask-socketio simple-websocket pyinstaller
pyinstaller --onefile --name Carrera3pilotos ^
  --add-data "templates;templates" --add-data "static;static" ^
  --hidden-import engineio.async_drivers.threading ^
  server.py
echo.
echo Listo: dist\Carrera3pilotos.exe
pause

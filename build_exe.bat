@echo off
REM Сборка single-file exe (MAX Desktop.exe).
REM Выходные пути в %TEMP% (ASCII), потому что путь проекта содержит кириллицу,
REM на которой PyInstaller иногда падает. Готовый exe копируется рядом с этим .bat.
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
  --name "MAX Desktop" ^
  --icon "%~dp0maxclient\assets\icon.ico" ^
  --add-data "%~dp0maxclient\assets;maxclient\assets" ^
  --distpath "%TEMP%\maxdist" --workpath "%TEMP%\maxbuild" --specpath "%TEMP%\maxbuild" ^
  run.py

copy /Y "%TEMP%\maxdist\MAX Desktop.exe" "%~dp0MAX Desktop.exe"
echo.
echo Готово: "%~dp0MAX Desktop.exe"
pause

@echo off
REM Builds two programs into dist\ :
REM   AndroidLite.exe      the window app (no console window)
REM   AndroidLite-cli.exe  the same app for scripts and terminals (prints to the console)
REM Needs Python 3.10+ on Windows. Run from this folder.
REM For the MSIX package, use packaging\build_msix.ps1 instead.

python -m pip install --upgrade pyinstaller || goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed --name AndroidLite ^
  --icon packaging\AndroidLite.ico main.py || goto :error

python -m PyInstaller --noconfirm --clean --onefile --console --name AndroidLite-cli ^
  --icon packaging\AndroidLite.ico main.py || goto :error

echo.
echo Build complete:
echo   dist\AndroidLite.exe
echo   dist\AndroidLite-cli.exe
exit /b 0

:error
echo Build failed.
exit /b 1
